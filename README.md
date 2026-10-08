# FBT Calibrator

#THIS IS VIBE CODED SOFTWARE, USE AT YOUR OWN RISK

I did not like the front end that motoc came with so I made my own! 

works on Arch-based Linux systems. might work on others but I haven't tested it

A dark, single-window GUI for calibrating full-body tracking with
`motoc` under WiVRn / Monado — built for Quest 3 +
Vive Tracker 3.0 setups on Linux, out of one too many evenings spent
fighting the command line in a headset.

![version](https://img.shields.io/badge/version-2.5-informational)

## What it does

- **Quick calibration** — source → target with your last-used device IDs,
  remembered between runs.
- **Custom calibration** — pick any source/target device IDs, or type IDs
  beyond the preset buttons.
- **Find Device IDs** — opens `motoc monitor` in your terminal emulator
  (whichever one you actually have) so you can watch devices appear.
- **Live device panel** — connected devices grouped by tracking origin,
  with IDs and serials, refreshed from `motoc show`. No devices or the
  service is down? It says so and keeps running instead of crashing.
- **Tracker battery badges** — per-device battery level and charging
  state, via the optional `fbt-battery` helper (below).
- **VRChat status** — a small live readout from status.vrchat.com at the
  bottom of the panel, independent of the tracking service.

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

## Install

```bash
git clone <this repo>
cd fbt-calibrator
./install.sh
```

The installer (no sudo) copies the app to
`~/.local/share/fbt-calibrator`, puts an `fbt-calibrator` command in
`~/.local/bin`, and generates a desktop launcher with the correct paths
for your machine — the `.desktop` file in this repo is only a template.
It also checks for python3/tkinter and warns if `motoc` is missing.

To uninstall, delete `~/.local/share/fbt-calibrator`,
`~/.local/bin/fbt-calibrator`, and
`~/.local/share/applications/calibrate-fbt.desktop`. Your settings live
in `~/.config/fbt-calibrator/config.json`.

## Battery badges (optional helper)

Battery levels come from a tiny Rust helper, `fbt-battery`, that talks
to Monado the same way `motoc` does. The GUI looks for it next to
`fbt-calibrator.py`; if it's absent or the service is down, badges are
simply hidden and everything else works.

A prebuilt binary may not match your Monado build, so building it
yourself is the reliable route:

```bash
cd battery-helper
cargo build --release
cp target/release/fbt-battery ..
```

See [battery-helper/README.md](battery-helper/README.md) for details.

## Notes for Vive 3.0 + Quest 3 users

- Device indices are **your** machine's enumeration order, not a fixed
  standard. Use Find Device IDs once, note which index is your headset
  and which are your trackers, and the app remembers them after the
  first calibration.
- Calibrate with WiVRn actually running and the trackers connected;
  the device panel will confirm what the service can see before you
  start.

## License

MIT — see [LICENSE](LICENSE).
