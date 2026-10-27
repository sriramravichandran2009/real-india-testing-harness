#!/usr/bin/env python3
"""
FieldReady Android condition harness — starter implementation.

Collects device/app evidence through ADB and produces JSON + Markdown reports.
Network presets are best-effort Android Emulator console commands.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"

def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")

def run_cmd(args: list[str], timeout: int = 20, check: bool = False) -> dict[str, Any]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        result = {"command": args, "returncode": p.returncode,
                  "stdout": p.stdout, "stderr": p.stderr}
        if check and p.returncode != 0:
            raise RuntimeError(f"Command failed: {' '.join(args)}\n{p.stderr}")
        return result
    except FileNotFoundError as e:
        return {"command": args, "returncode": 127, "stdout": "", "stderr": str(e)}
    except subprocess.TimeoutExpired as e:
        return {"command": args, "returncode": 124,
                "stdout": e.stdout or "", "stderr": f"Command timed out after {timeout}s"}

def adb(serial: str | None, *args: str, timeout: int = 20) -> dict[str, Any]:
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += list(args)
    return run_cmd(cmd, timeout=timeout)

def adb_text(serial: str | None, *args: str, timeout: int = 20) -> str:
    r = adb(serial, *args, timeout=timeout)
    return (r.get("stdout", "") + ("\n" + r.get("stderr", "") if r.get("stderr") else "")).strip()

def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", errors="replace")

def save_command_output(out_dir: Path, name: str, result: dict[str, Any]) -> None:
    write_text(out_dir / name, result.get("stdout", "") + (
        "\n\n--- STDERR ---\n" + result.get("stderr", "") if result.get("stderr") else ""
    ))

def choose_device(requested: str | None) -> str:
    result = run_cmd(["adb", "devices"], timeout=15)
    if result["returncode"] != 0:
        raise RuntimeError("ADB could not run. Install Android Platform Tools and put adb on PATH.")
    devices = []
    for line in result["stdout"].splitlines()[1:]:
        parts = line.strip().split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    if requested:
        if requested not in devices:
            raise RuntimeError(f"Requested device '{requested}' is not ready. `adb devices` shows: {devices}")
        return requested
    if len(devices) == 0:
        raise RuntimeError("No ready Android devices. Connect a phone with USB debugging or start an emulator.")
    if len(devices) > 1:
        raise RuntimeError(f"Multiple devices found {devices}; rerun with --serial DEVICE_ID.")
    return devices[0]

def apply_emulator_network(serial: str, profile: str) -> dict[str, Any]:
    # adb emu commands are implemented by Android Emulator console, not physical phones.
    presets = {
        "baseline": ["adb", "-s", serial, "emu", "network", "speed", "full"],
        "slow": ["adb", "-s", serial, "emu", "network", "speed", "edge"],
    }
    if profile == "baseline":
        cmds = [presets["baseline"], ["adb", "-s", serial, "emu", "network", "delay", "none"]]
    elif profile == "slow":
        cmds = [presets["slow"], ["adb", "-s", serial, "emu", "network", "delay", "1000"]]
    elif profile == "offline":
        # Best effort: simulator console support varies by emulator version.
        cmds = [["adb", "-s", serial, "emu", "network", "speed", "gsm"],
                ["adb", "-s", serial, "emu", "network", "delay", "none"]]
    else:
        return {"profile": profile, "status": "not_applied", "detail": "Unknown network profile"}
    results = [run_cmd(c, timeout=10) for c in cmds]
    ok = all(r["returncode"] == 0 for r in results)
    return {"profile": profile, "status": "commands_succeeded" if ok else "best_effort_or_unsupported",
            "commands": results,
            "note": "Verify actual app connectivity. Emulator console profiles are not a packet-loss simulator."}

def collect_device_info(serial: str) -> dict[str, str]:
    keys = {
        "manufacturer": "ro.product.manufacturer",
        "model": "ro.product.model",
        "android_release": "ro.build.version.release",
        "sdk": "ro.build.version.sdk",
        "abi": "ro.product.cpu.abi",
        "total_ram": "meminfo",  # collected separately
    }
    info = {}
    for label, prop in keys.items():
        if prop == "meminfo":
            info[label] = adb_text(serial, "shell", "cat", "/proc/meminfo")
        else:
            info[label] = adb_text(serial, "shell", "getprop", prop)
    info["serial"] = serial
    info["free_storage"] = adb_text(serial, "shell", "df", "-h", "/data")
    info["battery"] = adb_text(serial, "shell", "dumpsys", "battery")
    return info

def launch_app(serial: str, package: str, activity: str = "") -> dict[str, Any]:
    if activity.strip():
        component = activity.strip()
        if "/" not in component:
            component = f"{package}/{component}"
        return adb(serial, "shell", "am", "start", "-W", "-n", component, timeout=30)
    return adb(serial, "shell", "monkey", "-p", package, "-c",
               "android.intent.category.LAUNCHER", "1", timeout=30)

def score_findings(logcat: str, meminfo: str, ui_xml: str, launch_result: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    log_lower = logcat.lower()
    # Transparent heuristic: a log keyword is evidence to inspect, not proof of user impact.
    patterns = [
        (r"fatal exception|fatal signal", "Crash signature found in logcat", "critical", 4),
        (r"anr in |application not responding", "Possible ANR signature found in logcat", "high", 3),
        (r"outofmemoryerror|failed to allocate", "Possible memory exhaustion", "high", 3),
        (r"timed out|timeout", "Timeout text found in logs; inspect request context", "medium", 2),
    ]
    for pattern, title, severity, base in patterns:
        matches = re.findall(pattern, log_lower)
        if matches:
            findings.append({
                "title": title, "severity": severity, "evidence": f"{len(matches)} matching log occurrence(s)",
                "impact_score": base, "confidence": "medium",
                "reproduction": "Use the same scenario, app build, device serial/profile, and action sequence; inspect the attached logcat."
            })
    if launch_result.get("returncode", 1) != 0 or "error" in (launch_result.get("stderr", "").lower()):
        findings.append({
            "title": "App launch command reported an error", "severity": "high",
            "evidence": launch_result.get("stderr", "")[:500] or launch_result.get("stdout", "")[:500],
            "impact_score": 3, "confidence": "medium",
            "reproduction": "Install the same build, select the same device, then rerun the launch command from the report."
        })
    if not ui_xml.strip() or "hierarchy" not in ui_xml.lower():
        findings.append({
            "title": "UI hierarchy capture was empty or unavailable", "severity": "low",
            "evidence": "uiautomator dump did not return a recognizable hierarchy",
            "impact_score": 1, "confidence": "low",
            "reproduction": "Run the scenario again and inspect ui_hierarchy.xml; some secure/custom surfaces cannot be captured."
        })
    if not findings:
        findings.append({
            "title": "No rule-based failure signature detected in captured evidence",
            "severity": "info", "evidence": "No configured crash/ANR/memory/timeout pattern matched.",
            "impact_score": 0, "confidence": "low",
            "reproduction": "Repeat the scenario and add app-specific UI assertions. Absence of a log match does not prove the app is healthy."
        })
    return findings

def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        f"# FieldReady Run Report — {report['run_id']}",
        "",
        f"- **Started:** {report['started_at']}",
        f"- **Device:** {report['device'].get('manufacturer','')} {report['device'].get('model','')} (Android {report['device'].get('android_release','?')})",
        f"- **Serial:** `{report['device'].get('serial','')}`",
        f"- **Package:** `{report['scenario'].get('package','')}`",
        f"- **Network profile:** `{report['scenario'].get('network','baseline')}`",
        f"- **Artifacts directory:** `{report['artifact_dir']}`",
        "",
        "## Scenario actions",
    ]
    lines += [f"{i+1}. {action}" for i, action in enumerate(report["scenario"].get("actions", []))]
    lines += ["", "## Findings"]
    for f in sorted(report["findings"], key=lambda x: x.get("impact_score", 0), reverse=True):
        lines += [
            f"### [{f['severity'].upper()}] {f['title']}",
            f"- **Evidence:** {f.get('evidence','')}",
            f"- **Confidence:** {f.get('confidence','unknown')}",
            f"- **Impact score:** {f.get('impact_score',0)} (starter heuristic, not a measured user prevalence)",
            f"- **Reproduction:** {f.get('reproduction','')}",
            ""
        ]
    lines += [
        "## Interpretation limits",
        "- This report only reflects evidence captured during this run.",
        "- Rule-based log matches are leads for investigation, not proof of a defect.",
        "- National/user exposure ranking requires real product telemetry and a defensible sampling method.",
        "- Thermal behavior requires physical-device testing; this run does not claim to simulate thermal throttling.",
        ""
    ]
    return "\n".join(lines)

def main() -> int:
    parser = argparse.ArgumentParser(description="FieldReady Android condition harness")
    parser.add_argument("--scenario", required=True, help="Path to JSON scenario")
    parser.add_argument("--serial", help="ADB serial; required when more than one device is connected")
    parser.add_argument("--output", help="Optional output directory")
    args = parser.parse_args()

    scenario_path = Path(args.scenario)
    if not scenario_path.exists():
        print(f"Scenario not found: {scenario_path}", file=sys.stderr)
        return 2
    try:
        scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Could not read scenario JSON: {exc}", file=sys.stderr)
        return 2

    package = str(scenario.get("package", "")).strip()
    if not package or package == "com.example.myapp":
        print("Edit the scenario's 'package' to your installed app's package ID before running.", file=sys.stderr)
        return 2

    try:
        serial = choose_device(args.serial)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    out_dir = Path(args.output) if args.output else REPORTS / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    started = now_iso()

    # Clear previous logs so the run focuses on this scenario.
    adb(serial, "logcat", "-c", timeout=10)
    device = collect_device_info(serial)

    network = str(scenario.get("network", "baseline")).lower()
    network_result = apply_emulator_network(serial, network)
    (out_dir / "network_profile.json").write_text(json.dumps(network_result, indent=2), encoding="utf-8")

    # Capture package metadata and app launch result.
    package_info = adb(serial, "shell", "dumpsys", "package", package, timeout=20)
    save_command_output(out_dir, "package_info.txt", package_info)

    launch_result = launch_app(serial, package, str(scenario.get("launch_activity", "")))
    save_command_output(out_dir, "launch_result.txt", launch_result)
    time.sleep(max(1, min(int(scenario.get("wait_seconds", 15)), 120)))

    # Capture evidence.
    logcat = adb_text(serial, "logcat", "-d", "-v", "threadtime", timeout=30)
    write_text(out_dir / "logcat.txt", logcat)
    meminfo = adb_text(serial, "shell", "dumpsys", "meminfo", package, timeout=20)
    write_text(out_dir / "meminfo.txt", meminfo)
    write_text(out_dir / "df_data.txt", adb_text(serial, "shell", "df", "-h", "/data"))
    write_text(out_dir / "battery.txt", adb_text(serial, "shell", "dumpsys", "battery"))
    # Screen capture and UI hierarchy are best-effort.
    shot = adb(serial, "shell", "screencap", "-p", "/sdcard/fieldready_screen.png", timeout=20)
    if shot["returncode"] == 0:
        adb(serial, "pull", "/sdcard/fieldready_screen.png", str(out_dir / "screenshot.png"), timeout=30)
    hierarchy = adb(serial, "shell", "uiautomator", "dump", "/sdcard/window.xml", timeout=30)
    save_command_output(out_dir, "uiautomator_dump_command.txt", hierarchy)
    adb(serial, "pull", "/sdcard/window.xml", str(out_dir / "ui_hierarchy.xml"), timeout=30)

    # Process-death/relaunch test. This is a force-stop test, not a guaranteed low-memory OS kill.
    process_result = None
    relaunch_result = None
    if scenario.get("kill_and_relaunch", False):
        process_result = adb(serial, "shell", "am", "force-stop", package, timeout=15)
        time.sleep(2)
        relaunch_result = launch_app(serial, package, str(scenario.get("launch_activity", "")))
        time.sleep(5)
        save_command_output(out_dir, "force_stop_result.txt", process_result)
        save_command_output(out_dir, "relaunch_result.txt", relaunch_result)
        post_log = adb_text(serial, "logcat", "-d", "-v", "threadtime", timeout=30)
        write_text(out_dir / "logcat_after_relaunch.txt", post_log)

    findings = score_findings(logcat, meminfo, (out_dir / "ui_hierarchy.xml").read_text(encoding="utf-8", errors="replace")
                              if (out_dir / "ui_hierarchy.xml").exists() else "",
                              launch_result)
    report = {
        "run_id": run_id, "started_at": started, "finished_at": now_iso(),
        "artifact_dir": str(out_dir), "device": device, "scenario": scenario,
        "network_result": network_result, "launch_result": launch_result,
        "process_stop_result": process_result, "relaunch_result": relaunch_result,
        "findings": findings,
        "note": "Starter heuristics only; validate each finding and add app-specific UI assertions."
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    write_text(out_dir / "report.md", markdown_report(report))
    print(f"Run complete. Report: {out_dir / 'report.md'}")
    print(f"JSON evidence: {out_dir / 'report.json'}")
    print(f"Findings: {len(findings)}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
