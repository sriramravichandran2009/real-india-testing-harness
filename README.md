# FieldReady — Android Real-World Condition Testing Harness

A starter automation harness for collecting repeatable evidence from an Android app under constrained conditions.

## What it currently does
- Checks that ADB is installed and an Android device/emulator is connected.
- Records device model, Android version, RAM information, free storage, app package and build details where available.
- Launches an app and records logcat, screenshots, UI hierarchy XML, `dumpsys meminfo`, and `df` output.
- Supports emulator network presets (`adb emu network delay` and `adb emu network speed`) when the target is an Android Emulator.
- Runs a simple process-death/relaunch check and flags suspicious evidence using transparent heuristics.
- Writes a JSON report plus a human-readable Markdown report with a reproduction recipe.

## Requirements
- Python 3.10+
- Android Platform Tools (`adb`) installed and on PATH
- Android Emulator or an Android phone with USB debugging enabled
- The app installed on the target device
- The app package name, for example `com.example.myapp`

No third-party Python packages are required.

## Quick start

1. Unzip this project.
2. Connect your Android device with USB debugging enabled, or start an Android Emulator.
3. Confirm the device is visible:

   ```bash
   adb devices
   ```

4. Copy `scenarios/example.json` and edit `package`, `launch_activity` (optional), and the user journey notes.
5. Run a baseline:

   ```bash
   python harness.py --scenario scenarios/example.json --serial emulator-5554
   ```

   Omit `--serial` if exactly one device is connected.

6. Open the new folder created under `reports/`.

## Scenario file

`scenarios/example.json` is intentionally small. Set:
- `package`: your Android application package ID.
- `launch_activity`: optional explicit activity, such as `com.example.myapp/.MainActivity`.
- `network`: `baseline`, `slow`, or `offline`.
- `kill_and_relaunch`: whether to force-stop and relaunch the app after the initial evidence capture.
- `actions`: human-readable steps for the report. This starter does not automatically understand app-specific workflows; add UIAutomator/Appium steps for your actual app.

## Network profile limitations
- `baseline`, `slow`, and `offline` presets use Android Emulator console commands and work only when `adb emu` is supported by the selected emulator.
- `slow` uses a slow network preset and high delay; it does **not** model burst packet loss or realistic intermittent coverage by itself.
- `offline` is best-effort and emulator-version dependent. Check the command output and verify connectivity from inside the app.
- Physical Android devices generally need a controlled Wi-Fi access point, a network impairment gateway, or a test proxy for repeatable packet loss/intermittency. Running `tc netem` on an arbitrary laptop interface does not automatically shape the phone's traffic.
- Do not use privileged network shaping on a production network. Use a test device and test environment.

## What this is not
This is a usable starter harness, not a universal black-box app tester. It cannot reliably infer every silent no-op, broken image, or unusable screen from logs alone. Those checks need app-specific assertions, known screen elements, and/or UI automation. Thermal throttling must be measured on physical devices; emulators do not reproduce real device thermals.

## Suggested next steps
1. Add Appium or AndroidX UIAutomator tests for the core user journey.
2. Add app-side test hooks or accessibility IDs to assert loading/error/success states.
3. Use a physical low-end device for thermal and OEM battery-saver experiments.
4. Replace the sample priority score with a measured estimate based on your app's device/network telemetry, support tickets, and crash data.
5. Keep test accounts and redact secrets from logs and screenshots before sharing reports.
