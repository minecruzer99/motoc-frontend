# FBT Calibrator

A dark, single-window GUI for calibrating full-body tracking with
`motoc` under WiVRn / Monado — built for Quest 3 +
Vive Tracker 3.0 setups on Linux, out of one too many evenings spent
fighting the command line in a headset.

![version](https://img.shields.io/badge/version-4.3-informational)

## What it does

- **Quick calibration** — calibrates your last-used source/target pair,
  remembered between runs. The Ready screen shows that pair before
  you start, so there is no guessing what Quick will do.
- **Custom calibration** — pick any source/target device IDs, or type IDs
  beyond the preset buttons. Added IDs persist, and custom buttons can
  be deleted again. Source and target must be different devices.
- **Presets, by tracker serial** — save a pair by serial, not by
  enumeration index, so it still resolves when IDs shift. A recorded
  serial that is not connected stops the run instead of falling back
  to a possibly wrong old ID.
- **Calibrate all** — run every saved preset in order, with a Next /
  Queue complete flow on the result screen. Starting a manual run
  clears any aborted queue, so history is never mislabelled.
- **Pre-flight check** — motoc on PATH, Monado reachability and
  devices, VR service processes, and battery, in one screen.
- **Calibration history** — every run is recorded with time, IDs,
  serials when known, success or failure, and preset name.
- **Find Device IDs** — opens `motoc --wait monitor` in your terminal
  emulator (whichever one you actually have) so you can watch devices
  appear, even when WiVRn is not up yet. The menu also has a live
  device-ID panel using the same `--wait` monitor.
- **Live device panel** — connected devices grouped by tracking origin,
  with IDs and serials, refreshed from `motoc show`. No devices or the
  service is down? It says so and keeps running instead of crashing.
  Auto-refresh backs off from 20 seconds to 60 seconds after three
  consecutive service failures, and a successful poll schedules the
  next refresh at 20 seconds again. A new device is noted quietly in
  grey text only, never with a flash, sound, or popup.
- **Tracker battery badges** — per-device battery level and charging
  state, via the optional `fbt-battery` helper (below).
- **VRChat status** — a small live readout from status.vrchat.com at the
  bottom of the panel, independent of the tracking service.
- **Debug section** — Restart WiVRn / Monado, Check VR services, and
  Open calibration log, plus a manual Start WiVRn button. Start WiVRn
  is manual only: nothing in the app ever starts WiVRn automatically.
  The dashboard-launched notice auto-closes in 2 seconds.
- **Cancel and safe exit** — Countdown and Run screens have Cancel
  buttons, and closing the app terminates a calibration or monitor
  process instead of leaving it running.

## Requirements

- Linux with **WiVRn / Monado** running, and **`motoc` on your PATH**
  (the app drives `motoc calibrate` / `motoc show` under the hood and
  refuses to start without it).
- **python3 with tkinter**:
  - Arch: `sudo pacman -S tk`
  - Debian/Ubuntu: `sudo apt install python3-tk`
  - Fedora: `sudo dnf install python3-tkinter`
- Optional: a terminal emulator for Find Device IDs — `$TERMINAL` is
  honored first, then konsole, GNOME Terminal, kitty, alacritty, foot,
  xterm, and friends are tried in order.

## Install and launch (current workflow)

This is the recommended path. Do not use the legacy `install.sh`
below unless you specifically want v2.5 only.

1. Copy the whole folder (for example, to `~/Desktop/FBT Calibrator V3.0`).
   The folder name does not matter, but the files inside it do.
2. Run the setup script once, no sudo:

```bash
sh setup-switcher.sh
```

Setup finds the version switcher in the folder root or in a
"Python Scripts" subfolder, backs up any existing
`Calibrate-FBT.desktop`, writes a fresh launcher pointing at the
switcher with its path quoted, copies it to the app menu, and, when
`fbt-calibrator.png` is present in the root or in "Python Scripts",
copies it to `~/.local/share/icons/fbt-calibrator.png` and uses that
as the launcher icon.

3. Double-click `Calibrate-FBT.desktop`, or launch "Calibrate FBT"
   from the app menu, and pick a version from the menu. The switcher
   searches the root and "Python Scripts" for `fbt-calibrator*.py`
   files, reads their VERSION lines, and shows the folder for each
   version, so a root copy versus a "Python Scripts" copy is visible.
   Release files are named `fbt-calibrator-v<version>.py`, matching
   their VERSION, except for the unversioned `fbt-calibrator.py`,
   which is the v2.5 base build. Re-running setup after sorting or
   moving files repairs a launcher left pointing at a moved switcher.

## Sorting the folder

`sort-fbt-folder.sh` sorts loose files in the folder only:

```bash
sh sort-fbt-folder.sh --dry-run   # preview only
sh sort-fbt-folder.sh --yes       # sort without asking
```

Python files and `fbt-battery` go to "Python Scripts/", Rust battery
helper files go to "Battery Helper/", other loose Markdown files go to
"Docs/". Launchers, `setup-switcher.sh`, `install.sh`, `LICENSE`, and
`README.md` stay in the root, so README links and the installer
layout are preserved. Unknown files are left in place and listed,
same-name destinations are never overwritten, and the script runs
`setup-switcher.sh` at the end to repair the launcher.

## Battery badges (optional helper)

Battery levels come from a tiny Rust helper, `fbt-battery`, that talks
to Monado the same way `motoc` does. The GUI looks for it next to
the calibrator `.py` file it launched; if it's absent or the service
is down, badges are simply hidden and everything else works. That is
why the sorter puts the binary in "Python Scripts/" with the scripts,
not in "Battery Helper/".

A prebuilt binary may not match your Monado build, so building it
yourself is the reliable route:

```bash
cd battery-helper
cargo build --release
cp target/release/fbt-battery ..
```

See [battery-helper/README.md](battery-helper/README.md) for details.
Note: the helper source lives in `battery-helper/` with its own
`src/main.rs` layout; a flat "Battery Helper/" folder created by
sorting is not a Cargo project on its own.

## Legacy installer (v2.5 only)

`install.sh` is legacy. It installs only `fbt-calibrator.py` (v2.5)
to `~/.local/share/fbt-calibrator` and overwrites
`~/.local/share/applications/calibrate-fbt.desktop`, which is the
same app-menu launcher that `setup-switcher.sh` creates for the
version switcher. Running it after setup will silently downgrade the
app-menu entry to v2.5-only. It is kept only because it may be linked
from GitHub history. For current versions, use `setup-switcher.sh`.

To uninstall the legacy install, delete
`~/.local/share/fbt-calibrator`, `~/.local/bin/fbt-calibrator`, and
`~/.local/share/applications/calibrate-fbt.desktop`. Settings for
all versions live in `~/.config/fbt-calibrator/config.json`, with
calibration history in `~/.config/fbt-calibrator/history.json`.

## Notes for Vive 3.0 + Quest 3 users

- Device indices are **your** machine's enumeration order, not a fixed
  standard. Use Find Device IDs once, note which index is your headset
  and which are your trackers, and the app remembers them after the
  first calibration. Presets by serial avoid re-learning them when
  enumeration shifts.
- Calibrate with WiVRn actually running and the trackers connected;
  the device panel will confirm what the service can see before you
  start.

## License

MIT — see [LICENSE](LICENSE).
