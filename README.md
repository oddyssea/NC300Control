# NC300 Control

A local Windows utility for the BEBONCOOL NC300, with a graphite desktop interface, interactive controller diagrams, and direct access to the settings that work on this device. It reads USB/XInput for the live input display and uses the controller's separate Bluetooth LE connection for settings. The KeyLinker mobile app is not required.

## Run

Keep the controller powered and connected by USB, and turn on Windows Bluetooth. Open `NC300Control.exe` from the portable release (or `dist/NC300Control.exe` after building). It is a single portable Windows executable: no installer, Python installation, supporting `_internal` folder, or administrator access is needed. Put it in a writable folder. The app scans and connects automatically; **Connect controller** repeats discovery.

The EXE creates `NC300Control-data` beside itself for logs, backups, and display preferences. To move the app and keep its history, move the EXE and that data folder together. The bundled runtime extracts to a temporary Windows directory while running; the app's persistent data stays beside the EXE. First launch can take a few seconds.

Earlier builds stored data in `%LOCALAPPDATA%\NC300Control`. That data is preserved. To keep the old history and rear alignment, close the app and copy the old folder's contents into `NC300Control-data` beside the new EXE.

The source version runs with `Start-NC300.cmd`. On its first run, the launcher creates a project-local Python environment and installs Bleak from PyPI. Python 3.11 or newer is required for the source version; the packaged executable includes Python and its dependencies.

## Controls

- **Live input:** a front/rear controller diagram reacts to reported face buttons, D-pad, shoulders, triggers, stick clicks, and independently reported rear buttons. The sidebar shows the Xbox output received by Windows. Select the Windows player slot if more than one controller is connected.
- **Physical button feedback:** Bluetooth reports button presses before remapping, including the rear buttons. This firmware test session runs only while the app is in the foreground on Live input or Remapping, and stops on backgrounding, other tabs, or closing. The firmware may vibrate during bumper tests. Turn off Physical button feedback to use Xbox-output monitoring alone.
- **Rear alignment:** if your unit's two rear indicators appear reversed, use **Swap rear indicators**. This saves a local display preference and does not change controller assignments.
- **Remapping:** click a control on the diagram, choose its new action, and click **Apply mapping**. Keyboard dropdowns and the assignments table remain available. Choices come from the controller's own supported-button lists. **Original button** restores the chosen button's normal action. Rear macro buttons are shown live but are not exposed as ordinary remapping sources by this firmware.
- **Settings:** adjust each stick/trigger's inner deadzone and the two vibration strengths. Other curve points, flags, idle timers, and unedited settings are preserved.
- **Undo last change:** restore the preceding record, then verify it by reading the controller again. Undo history lasts for the current app session; the JSON backups remain on disk.

Settings are read on connection. A setting changes when you click **Apply**. Every change requires a fresh read, a disk backup, acknowledgements for all packets, and a matching read-back. An interrupted or failed change is reported as a failure; Undo and the saved backup remain available.

## Compatibility and privacy

Writes are enabled only for the NC300 identity measured on this PC: vendor `1038`, product `2003`, firmware `1.06`, operating mode `6`, identity record `10382003010656`. Other firmware remains read only. Bluetooth configuration is separate from the generic Xbox USB identity.

The app makes no internet requests, has no account, and uploads no data. The portable build stores diagnostics and backups in `NC300Control-data` beside the EXE; source runs use `%LOCALAPPDATA%\NC300Control`. Nearby device information is used only for local controller discovery.

Turbo and macro records can be captured in backups, but this initial utility edits remapping, deadzones, and motor strengths. Firmware flashing, factory reset, and motion calibration are not exposed.

## Verified on the connected controller

- Read identity, capability lists, supported buttons, current curves, vibration, turbo, and back-button records.
- Temporarily changed and read back motor strengths, a stick deadzone, a trigger deadzone, and A-to-B remapping.
- Restored each original setting, disconnected, reconnected, and confirmed the restored records.
- Queried the connected USB/XInput slot successfully.
- Captured actual A/B/X/Y input and separate rear-button press/release packets. Replayed captures through the physical-state decoder and diagram tests.
- Tested diagram selection, physical-versus-Xbox input separation, foreground monitoring lifecycle, and native desktop navigation.

The read-back tests verify the configuration records. A physical button test is still needed to confirm how each remapping behaves in a particular game. A full controller power cycle has not been tested.

## Develop and test

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m nc300 --smoke-test
.\Build.ps1
```

`tools/validate_hardware.py --address <controller-address>` performs read-only checks. Adding `--exercise` temporarily tests four settings, saves the baseline, restores each record, and checks again after reconnecting. It is an explicit hardware test, not part of normal startup.

`tools/observe_inputs.py --address <controller-address> --duration 90` runs a bounded physical-input test and verifies that saved settings remain unchanged afterward. This explicitly enters the firmware's button-test mode and always sends cleanup on completion.

## GitHub distribution

Publish the contents of this project directory as the source repository. Build products, local environments, validation captures, controller backups, and portable data are excluded by `.gitignore`. Do not publish the surrounding research/workspace directory.

`Build.ps1` produces `dist/NC300Control.exe`, `dist/NC300Control-source.zip`, `dist/NC300Control-portable.zip`, and `dist/SHA256SUMS.txt`. Use the portable ZIP and checksums as GitHub release assets. The ZIP includes the executable, corresponding source, documentation, and dependency licenses. The EXE can run on its own after extraction. License notices are also embedded in the executable.

## License and sources

GNU GPL version 3 or later; see `LICENSE` and `THIRD_PARTY.md`. The source is included with this workspace and the distribution's source archive. This utility is independent of BEBONCOOL, ZhiXu, and x20ctl.

- [Controller manual](https://manuals.plus/ae/3256811601105752)
- [Original manual PDF](https://fccid.io/m/4d98cd41b95be6d818986fc096f6f30b7266ad9f9c29c0709e51ed3f3c5bc21c.pdf)
- [x20ctl protocol research and source](https://github.com/AmjadAAYD/x20ctl)
- [Bleak](https://github.com/hbldh/bleak)
