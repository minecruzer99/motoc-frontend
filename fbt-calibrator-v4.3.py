#!/usr/bin/env python3
"""WiVRn FBT Calibration GUI, v4.3.

v4.3 is the audit cleanup pass: it fixes the queue leak, src==dst, Quick card wording, UI freezes, corrupt-config loss, backoff timing, highlight, cancel, and destroy handling, with no behaviour change to presets, Start WiVRn (manual only), or the silent new-device note.

WiVRn FBT Calibration GUI, v4.

A dark, VR-friendly front end for `motoc calibrate`.

v4 adds, on top of v3:
- Serial-based presets: save a source/target pair by tracker
  serial, not by enumeration index, so it still resolves when
  IDs shift. Presets live in config, under "presets".
- Calibrate-all: run every saved preset in order, from the
  Presets screen, with a Next/Finish flow on the result screen.
- Pre-flight check: motoc, Monado reachability, devices, and
  battery, in one screen, before a calibration run.
- Calibration history: every run is appended to history.json
  (timestamp, IDs, serials when known, success, preset name)
  and shown on the History screen.
- Start WiVRn button in Debug: MANUAL ONLY. Nothing in this app
  ever starts WiVRn automatically; the button launches the
  WiVRn dashboard when you press it, and only then.
- Silent new-device note: when the device panel sees a serial
  it has not seen before, it shows a quiet DIM-coloured
  "New device: ..." line in the panel. No flash, no ping,
  no sound, no popup.

Flow: menu -> custom pick -> ready -> countdown -> calibrating -> result.
The right-hand "Device status" panel lists live tracking devices via
`motoc show` and handles the no-devices / service-offline cases gracefully.
If a `fbt-battery` helper sits next to this script, per-device battery
levels are shown in the device list. A "VRChat API" section at the bottom
of the panel shows live service status from status.vrchat.com.

Requires: python3 with tkinter, and `motoc` on PATH.
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tempfile
import time
import tkinter as tk
import urllib.request
from tkinter import messagebox, ttk

# ---------------------------------------------------------------- palette
VERSION = "4.3"
BG = "#101018"
CARD = "#1a1a28"
CARD_HOVER = "#26263a"
SIDEBAR_BG = "#14141d"
FG = "#f0f0fa"
DIM = "#9a9ab5"
ACCENT = "#00e5ff"
ACCENT_BG = "#0a2a33"
GOOD = "#00e676"
BAD = "#ff5252"
SANS = "DejaVu Sans"
MONO = "DejaVu Sans Mono"

# Per-user temp paths: a shared bare /tmp name would collide (and be
# clobbered) between users on a multi-user system.
_UID = os.getuid() if hasattr(os, "getuid") else 0
LOG_PATH = os.path.join(tempfile.gettempdir(),
                        "motoc_log_{}.txt".format(_UID))
ERROR_LOG_PATH = os.path.join(tempfile.gettempdir(),
                              "fbt-calibrator-error-{}.log".format(_UID))
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".config",
                           "fbt-calibrator", "config.json")
HISTORY_PATH = os.path.join(os.path.expanduser("~"), ".config",
                            "fbt-calibrator", "history.json")


def battery_helper_path():
    """Path to the `fbt-battery` helper, or None if it isn't installed."""
    here = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(here, "fbt-battery")
    return p if os.path.isfile(p) and os.access(p, os.X_OK) else None


# Terminals tried for "Find Device IDs", in order after $TERMINAL.
# Each entry: binary -> argv fragment that runs a command inside it.
# The original build hardcoded konsole + fish, which only exists on
# the author's machine; everyone else just got an error dialog.
TERMINALS = [
    ("konsole", ["-e"]),
    ("gnome-terminal", ["--"]),
    ("kgx", ["--"]),
    ("ptyxis", ["--"]),
    ("kitty", ["-e"]),
    ("alacritty", ["-e"]),
    ("wezterm", ["start", "--"]),
    ("foot", []),
    ("xfce4-terminal", ["-e"]),
    ("mate-terminal", ["-e"]),
    ("tilix", ["-e"]),
    ("xterm", ["-e"]),
]


def open_in_terminal(command):
    """Run `command` in the first terminal emulator found.

    Returns True on success. The command runs under `sh` (present on
    every Linux system) and the window waits for Enter afterwards so
    the output stays readable once the command exits.
    """
    shell_line = (command + "; echo; "
                  "echo 'Press Enter to close this window.'; read -r _")
    candidates = []
    env_term = os.environ.get("TERMINAL")
    if env_term:
        candidates.append((env_term, ["-e"]))
    candidates.extend(TERMINALS)
    for binary, prefix in candidates:
        path = shutil.which(binary)
        if not path:
            continue
        try:
            subprocess.Popen([path] + prefix + ["sh", "-c", shell_line])
            return True
        except OSError:
            continue
    return False


# ------------------------------------------------------- debug helpers
# v3 debug section. WiVRn on this machine is dashboard-spawned, with no
# systemd user unit, so "restart" honestly means: stop the running
# server process here, then start it again from the WiVRn dashboard.
# Guessing a systemctl restart or launching a second server from this
# app would either fail or fight the dashboard, so this does neither.
SERVICE_PROCS = ["wivrn-server", "monado-service"]


def find_service_pids():
    """{process_name: [pid, ...]} for the VR services, {} when none run."""
    found = {}
    for name in SERVICE_PROCS:
        try:
            out = subprocess.run(["pgrep", "-x", name], capture_output=True,
                                 text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        pids = [p.strip() for p in out.stdout.splitlines() if p.strip()]
        if pids:
            found[name] = pids
    return found


def service_status_lines():
    """One human-readable line per service, plus where motoc lives."""
    lines = []
    found = find_service_pids()
    for name in SERVICE_PROCS:
        if name in found:
            lines.append("{}: running (pid {})".format(
                name, ", ".join(found[name])))
        else:
            lines.append("{}: not running".format(name))
    motoc = shutil.which("motoc")
    lines.append("motoc: {}".format(motoc if motoc else "not on PATH"))
    return lines


def stop_service_pids(pids):
    """SIGTERM, wait up to 5 s, then SIGKILL what is left. Returns survivors."""
    import signal
    for pid in pids:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (OSError, ValueError):
            continue
    deadline = time.time() + 5
    survivors = list(pids)
    while survivors and time.time() < deadline:
        time.sleep(0.25)
        still = []
        for pid in survivors:
            try:
                os.kill(int(pid), 0)
            except OSError:
                continue
            still.append(pid)
        survivors = still
    for pid in survivors:
        try:
            os.kill(int(pid), signal.SIGKILL)
        except (OSError, ValueError):
            continue
    return survivors


def primary_monitor_geometry():
    """(x, y, w, h) of the primary monitor from xrandr, or None.

    Tk's winfo_screenwidth() is the whole virtual desktop across
    every monitor, so centering on that lands the window on
    whichever monitor happens to sit in the middle, not the primary
    one. xrandr --listmonitors marks the primary monitor with *.
    """
    try:
        out = subprocess.run(["xrandr", "--listmonitors"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    pat = re.compile(r"^\s*\d+:\s+\+(\*?)(\S+)\s+(\d+)/\d+x(\d+)/\d+"
                     r"\+(\d+)\+(\d+)")
    for line in out.stdout.splitlines():
        m = pat.search(line)
        if not m:
            continue
        geom = (int(m.group(5)), int(m.group(6)),
                int(m.group(3)), int(m.group(4)))
        if m.group(1) == "*":
            return geom
    return None  # no * marker: do not guess, use the old centering


def read_battery():
    """{device_index: (percent, charging)} — {} when unavailable."""
    helper = battery_helper_path()
    if not helper:
        return {}
    try:
        out = subprocess.run([helper], capture_output=True, text=True,
                             timeout=15)
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}
    batt = {}
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        idx, pct, chg = parts
        if pct == "-":
            continue
        try:
            batt[idx] = (int(pct), chg == "1")
        except ValueError:
            continue
    return batt


# ------------------------------------------------- VRChat API status page
VRC_STATUS_URL = "https://status.vrchat.com/api/v2/status.json"
VRC_COMPONENTS_URL = "https://status.vrchat.com/api/v2/components.json"
VRC_CACHE_S = 300  # status pages barely change; don't hammer the API
_vrc_cache = {"at": 0.0, "data": None}

VRC_COMPONENT_STYLE = {
    "operational": ("Operational", "#4ade80"),
    "degraded_performance": ("Degraded", "#fbbf24"),
    "partial_outage": ("Partial outage", "#fb923c"),
    "major_outage": ("Major outage", "#f87171"),
    "under_maintenance": ("Maintenance", "#7dd3fc"),
}
VRC_INDICATOR_COLOR = {
    "none": "#4ade80",
    "minor": "#fbbf24",
    "major": "#fb923c",
    "critical": "#f87171",
}


def fetch_vrc_status():
    """(dot_color, headline, [(name, label, color), ...]) or None on failure."""
    try:
        with urllib.request.urlopen(VRC_STATUS_URL, timeout=10) as r:
            status = json.load(r)["status"]
        with urllib.request.urlopen(VRC_COMPONENTS_URL, timeout=10) as r:
            components = json.load(r)["components"]
    except Exception:  # noqa: BLE001 - offline just hides the section
        return None
    by_id = {c["id"]: c for c in components}
    group = next((c for c in components
                  if c.get("group") and c.get("name") == "API / Website"), None)
    if group:
        leaves = [by_id[i] for i in group.get("components", []) if i in by_id]
    else:
        leaves = [c for c in components if not c.get("group")]
    leaves.sort(key=lambda c: c.get("position", 0))
    rows = []
    for c in leaves:
        label, color = VRC_COMPONENT_STYLE.get(
            c.get("status"), (c.get("status", "?").replace("_", " ").title(),
                              DIM))
        rows.append((c.get("name", "?"), label, color))
    indicator = status.get("indicator", "none")
    return (VRC_INDICATOR_COLOR.get(indicator, DIM),
            status.get("description", "Unknown status"),
            rows)


def read_vrc_status():
    """Cached VRC status; keeps the last good readout on transient failure."""
    now = time.time()
    cached = _vrc_cache["data"]
    if cached is not None and now - _vrc_cache["at"] < VRC_CACHE_S:
        return cached
    data = fetch_vrc_status()
    if data is None:
        return cached
    _vrc_cache["at"] = now
    _vrc_cache["data"] = data
    return data


def style_button(b, bg=CARD, fg=FG):
    # _current_bg is what Leave restores. PickScreen.update_go sets it
    # for selected vs unselected ID buttons, so hovering a selected
    # button no longer erases its selection highlight.
    b._current_bg = bg
    b.configure(bg=bg, fg=fg, activebackground=CARD_HOVER,
                activeforeground=ACCENT, relief="flat", bd=0,
                highlightthickness=1, highlightbackground="#33334a",
                cursor="hand2")
    b.bind("<Enter>", lambda _e: b.configure(bg=CARD_HOVER))
    b.bind("<Leave>", lambda _e: b.configure(
        bg=getattr(_e.widget, "_current_bg", bg)))


# ------------------------------------------------------- motoc show parse
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
ORIGIN_RE = re.compile(r"^\[(\d+)\]\s*(.*?)\s*$")
DEV_RE = re.compile(r"^[├└]──\s*\[(\d+)\]\s*\"([^\"]+)\"(?:\s*\((.*?)\))?\s*$")


def parse_motoc_show(output):
    """Parse `motoc show` into [(origin_name, [(index, serial, name), ...])].

    Sample input::

        [0] WiVRn
         │ POS: (X: 0.00, Y: 0.00, Z: 0.00)
         ├── [1] "ABC123" (Left Controller)
         └── [2] "XYZ789"
        [1] Lighthouse
         └── [7] "LHR-ABCDE000" (Vive Tracker)
    """
    origins = []
    current = None
    for raw in output.splitlines():
        line = ANSI_RE.sub("", raw).strip()
        if not line:
            continue
        m = DEV_RE.match(line)
        if m and current is not None:
            current[1].append((m.group(1), m.group(2), (m.group(3) or "").strip()))
            continue
        m = ORIGIN_RE.match(line)
        if m:
            current = (m.group(2) or "Origin {}".format(m.group(1)), [])
            origins.append(current)
    return origins


# ------------------------------------------------------- v4 presets/history
def motoc_show_origins():
    """parse_motoc_show(motoc show) or None when unreachable."""
    try:
        proc = subprocess.run(["motoc", "show"], capture_output=True,
                              text=True, timeout=12)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return parse_motoc_show(proc.stdout or "")
    except Exception:
        return None


def serial_maps(origins):
    """({serial: index}, {index: serial}) from parsed motoc show."""
    by_serial, by_index = {}, {}
    for _origin, devs in origins or []:
        for idx, serial, _name in devs:
            by_serial[serial] = idx
            by_index[idx] = serial
    return by_serial, by_index


def resolve_preset_from_origins(origins, preset):
    """Resolve a preset against one motoc show snapshot.

    Same rules as resolve_preset, but without a new subprocess call,
    so Calibrate-all can resolve every preset from a single snapshot.
    """
    if origins is None:
        return None, None, ["Monado unreachable"]
    by_serial, _by_index = serial_maps(origins)
    out, missing = [], []
    for serial_key, id_key in (("src_serial", "src_id"),
                               ("dst_serial", "dst_id")):
        serial = preset.get(serial_key) or ""
        if serial and serial in by_serial:
            out.append(by_serial[serial])
        elif serial:
            missing.append(serial)
        elif preset.get(id_key):
            out.append(str(preset[id_key]))
        else:
            missing.append("?")
    if missing:
        return None, None, missing
    return out[0], out[1], []


def _backup_corrupt(path):
    try:
        stamp = time.strftime("%Y%m%d%H%M%S")
        backup = "{}.corrupt.{}".format(path, stamp)
        os.replace(path, backup)
        return backup
    except OSError:
        return None


def _write_json_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "{}.tmp.{}".format(path, os.getpid())
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def resolve_preset(preset):
    """Resolve a preset to (src_id, dst_id, missing_serials).

    Serials win: if a saved serial is currently connected, its
    current index is used, even if enumeration shifted since the
    preset was saved. A saved ID is only a fallback when no serial
    was recorded. Missing means the serial is not connected now,
    so calibrating would pick the wrong device, and the caller
    should not start.
    """
    return resolve_preset_from_origins(motoc_show_origins(), preset)


def load_history():
    """[history entries], newest last; [] on missing/corrupt file.

    A corrupt file is backed up to history.json.corrupt.<timestamp>
    before returning [], so a later append cannot silently destroy
    the old entries without a copy surviving.
    """
    try:
        with open(HISTORY_PATH) as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        _backup_corrupt(HISTORY_PATH)
        return []
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        if os.path.isfile(HISTORY_PATH):
            _backup_corrupt(HISTORY_PATH)
        return []


def append_history(entry):
    """Append one history entry, capped at 100, ignoring write errors."""
    history = load_history()
    history.append(entry)
    history = history[-100:]
    try:
        _write_json_atomic(HISTORY_PATH, history)
    except OSError:
        pass


# ---------------------------------------------------------------- screens
class Screen(tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, bg=BG)
        self.app = app

    def on_show(self):
        pass


class MenuScreen(Screen):
    def __init__(self, parent, app):
        super().__init__(parent, app)
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(expand=True, fill="both", padx=48, pady=28)

        tk.Label(wrap, text="WiVRn FBT", font=(SANS, 32, "bold"),
                 bg=BG, fg=FG).pack(anchor="w")
        tk.Label(wrap, text="Full-body tracking calibration", font=(SANS, 14),
                 bg=BG, fg=DIM).pack(anchor="w", pady=(0, 26))

        self.card(wrap, "Quick Calibrate",
                  "Last-used pair, shown on the Ready screen", self.quick)
        self.card(wrap, "Custom Calibrate",
                  "Pick your own source & target device IDs", self.custom)
        self.card(wrap, "Presets",
                  "Saved pairs by tracker serial, plus Calibrate all",
                  lambda: self.app.show("PresetsScreen"))
        self.card(wrap, "Pre-flight check",
                  "motoc, Monado, devices, and battery, before you start",
                  lambda: self.app.show("PreflightScreen"))
        self.card(wrap, "History",
                  "Past calibration runs, with success or fail",
                  lambda: self.app.show("HistoryScreen"))
        self.card(wrap, "Find Device IDs",
                  "Open motoc monitor to list connected devices", self.find_ids)

        exit_b = tk.Button(wrap, text="Exit", font=(SANS, 14),
                           pady=8, command=self.app.destroy)
        style_button(exit_b)
        exit_b.pack(fill="x", pady=(16, 0))

        # v3.1 live device-ID panel: this fills the empty space that
        # used to sit under Exit. It auto-starts on the menu, because
        # finding IDs is the first step almost every time, and it
        # runs `motoc --wait monitor` (not plain `motoc monitor`) so
        # it waits for Monado instead of dying with "Monado is not
        # reachable" when WiVRn is not up yet. The Find Device IDs
        # card above now opens the same --wait monitor in a terminal
        # if you want the full terminal view.
        live = tk.Frame(wrap, bg=BG)
        live.pack(fill="both", expand=True, pady=(20, 0))
        head = tk.Frame(live, bg=BG)
        head.pack(fill="x", pady=(0, 6))
        tk.Label(head, text="Live device IDs", font=(SANS, 14, "bold"),
                 bg=BG, fg=FG, anchor="w").pack(side="left")
        tk.Label(head, text="motoc --wait monitor",
                 font=(MONO, 10), bg=BG, fg=DIM, anchor="w").pack(
                     side="left", padx=(10, 0))
        restart_b = tk.Button(head, text="\u21bb Restart monitor",
                              font=(SANS, 11),
                              command=self.restart_monitor)
        style_button(restart_b)
        restart_b.pack(side="right")
        self.monitor_text = tk.Text(live, font=(MONO, 11),
                                    bg="#0a0a12", fg="#c8c8e0",
                                    relief="flat", highlightthickness=1,
                                    highlightbackground="#33334a",
                                    state="disabled")
        self.monitor_text.pack(fill="both", expand=True)
        self.monitor_proc = None
        self.monitor_q = queue.Queue()
        self._monitor_poll_job = None
        self._monitor_thread = None
        self._monitor_stopped = False

        tk.Label(wrap, text="powered by motoc", font=(SANS, 11),
                 bg=BG, fg=DIM).pack(side="bottom", pady=(20, 0))

    # -- live monitor -------------------------------------------------
    def on_show(self):
        self.start_monitor()

    def on_hide(self):
        self.stop_monitor()

    def start_monitor(self):
        self.stop_monitor()
        # Drain stale lines/done items from the previous monitor,
        # or the new poll loop could exit on the old done item.
        while True:
            try:
                self.monitor_q.get_nowait()
            except queue.Empty:
                break
        self._monitor_stopped = False
        self._append_monitor("Waiting for Monado / devices...\n")
        self._monitor_thread = threading.Thread(target=self._monitor_worker,
                                                daemon=True)
        self._monitor_thread.start()
        self._monitor_poll()

    def restart_monitor(self):
        self.start_monitor()

    def stop_monitor(self):
        self._monitor_stopped = True
        if self._monitor_poll_job:
            try:
                self.after_cancel(self._monitor_poll_job)
            except tk.TclError:
                pass
            self._monitor_poll_job = None
        if self.monitor_proc and self.monitor_proc.poll() is None:
            try:
                self.monitor_proc.terminate()
            except OSError:
                pass
        self.monitor_proc = None

    def _monitor_worker(self):
        try:
            if self._monitor_stopped:
                return
            self.monitor_proc = subprocess.Popen(
                ["motoc", "--wait", "monitor"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1)
            if self._monitor_stopped:
                try:
                    self.monitor_proc.terminate()
                except OSError:
                    pass
                return
            for line in self.monitor_proc.stdout:
                self.monitor_q.put(("line", line))
            self.monitor_proc.wait()
            self.monitor_q.put(("done", self.monitor_proc.returncode))
        except FileNotFoundError:
            self.monitor_q.put(("line", "error: 'motoc' was not found.\n"))
            self.monitor_q.put(("done", 127))
        except Exception as e:  # noqa: BLE001 - surfaced in the panel
            self.monitor_q.put(("line", "error: {}\n".format(e)))
            self.monitor_q.put(("done", 1))

    def _append_monitor(self, text):
        self.monitor_text.configure(state="normal")
        self.monitor_text.insert("end", text)
        # Keep the panel bounded: drop the oldest lines past 400.
        try:
            lines = int(self.monitor_text.index("end-1c").split(".")[0])
        except (tk.TclError, ValueError):
            lines = 0
        if lines > 400:
            self.monitor_text.delete("1.0", "101.0")
        self.monitor_text.see("end")
        self.monitor_text.configure(state="disabled")

    def _monitor_poll(self):
        try:
            while True:
                kind, payload = self.monitor_q.get_nowait()
                if kind == "line":
                    self._append_monitor(payload)
                else:
                    if payload != 0:
                        self._append_monitor(
                            "\n[monitor exited, code {} - hit Restart "
                            "monitor to try again]\n".format(payload))
                    self._monitor_poll_job = None
                    return
        except queue.Empty:
            pass
        self._monitor_poll_job = self.after(150, self._monitor_poll)

    def card(self, parent, title, desc, cmd):
        f = tk.Frame(parent, bg=CARD)
        f.pack(fill="x", pady=8)
        b = tk.Button(f, text=title, font=(SANS, 17, "bold"),
                      anchor="w", padx=22, pady=8, command=cmd)
        style_button(b)
        b.pack(fill="x")
        # NOTE: keep pady as a single value here -- a (top, bottom) tuple is
        # only legal on pack/grid, not on the widget constructor itself, and
        # crashes on some Tk builds.
        s = tk.Label(f, text=desc, font=(SANS, 12), bg=CARD, fg=DIM,
                     anchor="w", padx=24)
        s.pack(fill="x", pady=(0, 12))
        for w in (f, s):
            w.bind("<Button-1>", lambda _e: cmd())
            w.configure(cursor="hand2")

    def quick(self):
        # app.src/app.dst already hold the last-used IDs (loaded from
        # config at startup); the very first run falls back to 1 -> 7.
        self.app.show("ReadyScreen")

    def custom(self):
        self.app.show("PickScreen")

    def find_ids(self):
        if not open_in_terminal("motoc --wait monitor"):
            messagebox.showerror(
                "No terminal found",
                "Couldn't find a terminal emulator to run "
                "'motoc --wait monitor' in.\n\n"
                "Run it yourself in any terminal:\n\n"
                "    motoc --wait monitor")


class HoverTip:
    """Show `text` in a small popup after hovering for `delay_ms`.

    Bindings use add="+" so they never replace a button's own hover
    styling (style_button's Enter/Leave background swap). The popup
    hides on leave, on click, or when its timer is cancelled.
    """

    def __init__(self, widget, text, delay_ms=3000):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self.tip = None
        self.job = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress-1>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._hide()
        self.job = self.widget.after(self.delay_ms, self._show)

    def _show(self):
        self.job = None
        if self.tip is not None:
            return
        try:
            x = self.widget.winfo_rootx() + 20
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        except tk.TclError:
            return
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry("+{}+{}".format(x, y))
        tk.Label(tip, text=self.text, font=(SANS, 11), bg="#222233",
                 fg=FG, relief="solid", borderwidth=1,
                 padx=8, pady=4).pack()
        self.tip = tip

    def _hide(self, _event=None):
        if self.job is not None:
            try:
                self.widget.after_cancel(self.job)
            except tk.TclError:
                pass
            self.job = None
        if self.tip is not None:
            try:
                self.tip.destroy()
            except tk.TclError:
                pass
            self.tip = None


class PickScreen(Screen):
    SRC_IDS = ["1", "2"]
    DST_IDS = ["3", "4", "5", "6", "7"]

    def __init__(self, parent, app):
        super().__init__(parent, app)
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(expand=True, fill="both", padx=48, pady=28)

        tk.Label(wrap, text="Custom calibration", font=(SANS, 26, "bold"),
                 bg=BG, fg=FG).pack(anchor="w", pady=(0, 18))

        # v3.2: buttons added with "Add as button" persist in config,
        # so they come back next launch instead of being re-typed.
        cfg = app._load_config()
        self.src_ids = list(self.SRC_IDS)
        self.dst_ids = list(self.DST_IDS)
        for value in cfg.get("custom_src_ids", []) or []:
            if (isinstance(value, str) and value.isdigit()
                    and value not in self.src_ids):
                self.src_ids.append(value)
        for value in cfg.get("custom_dst_ids", []) or []:
            if (isinstance(value, str) and value.isdigit()
                    and value not in self.dst_ids):
                self.dst_ids.append(value)

        self.src_var = tk.StringVar(
            value=app.src if app.src in self.src_ids else self.SRC_IDS[0])
        self.dst_var = tk.StringVar(
            value=app.dst if app.dst in self.dst_ids else self.DST_IDS[0])

        tk.Label(wrap, text="Source device ID", font=(SANS, 15),
                 bg=BG, fg=DIM).pack(anchor="w", pady=(0, 8))
        self.src_btns, self.src_entry, self.src_canvas = self._id_row(
            wrap, "src", self.src_ids, self.src_var)
        if app.src not in self.src_ids:
            self.src_entry.insert(0, app.src)

        tk.Label(wrap, text="Target device ID", font=(SANS, 15),
                 bg=BG, fg=DIM).pack(anchor="w", pady=(0, 8))
        self.dst_btns, self.dst_entry, self.dst_canvas = self._id_row(
            wrap, "dst", self.dst_ids, self.dst_var)
        if app.dst not in self.dst_ids:
            self.dst_entry.insert(0, app.dst)

        go = tk.Button(wrap, text="Continue  \u2192", font=(SANS, 18, "bold"),
                       pady=10, command=self.go)
        style_button(go, bg=ACCENT_BG, fg=ACCENT)
        go.pack(fill="x", pady=(0, 12))
        back = tk.Button(wrap, text="\u2190 Back", font=(SANS, 13),
                         command=lambda: self.app.show("MenuScreen"))
        style_button(back)
        back.pack()
        # v4 save-preset row: name the pair, save it by serial when
        # the devices are currently connected, else by ID as a
        # fallback. The Presets screen resolves serials at run time.
        save_frame = tk.Frame(wrap, bg=BG)
        save_frame.pack(fill="x", pady=(20, 0))
        tk.Label(save_frame, text="Save this pair as preset:",
                 font=(SANS, 12), bg=BG, fg=DIM).pack(side="left")
        self.preset_name_entry = tk.Entry(save_frame, font=(SANS, 13),
                                          width=20, bg="#0a0a12", fg=FG,
                                          insertbackground=ACCENT,
                                          relief="flat",
                                          highlightthickness=1,
                                          highlightbackground="#33334a",
                                          highlightcolor=ACCENT)
        self.preset_name_entry.pack(side="left", padx=(10, 0))
        save_b = tk.Button(save_frame, text="Save preset",
                           font=(SANS, 12),
                           command=self.save_preset)
        style_button(save_b, bg=ACCENT_BG, fg=ACCENT)
        save_b.pack(side="left", padx=(10, 0))
        self.update_go()

    def save_preset(self):
        src = self.effective(self.src_var, self.src_entry)
        dst = self.effective(self.dst_var, self.dst_entry)
        if not re.fullmatch(r"\d+", src) or not re.fullmatch(r"\d+", dst):
            messagebox.showerror("Invalid device ID",
                                 "Pick valid source and target IDs first.")
            return
        if src == dst:
            messagebox.showerror("Invalid device ID",
                                 "Source and target must be different "
                                 "devices.")
            return
        name = self.preset_name_entry.get().strip() or \
            "{} \u2192 {}".format(src, dst)
        origins = motoc_show_origins()
        _by_serial, by_index = serial_maps(origins) if origins else ({}, {})
        preset = {
            "name": name,
            "src_serial": by_index.get(src, ""),
            "dst_serial": by_index.get(dst, ""),
            "src_id": src,
            "dst_id": dst,
        }
        cfg = self.app._load_config()
        presets = [p for p in (cfg.get("presets", []) or [])
                   if isinstance(p, dict) and p.get("name") != name]
        presets.append(preset)
        self.app._save_config(presets=presets)
        self.preset_name_entry.delete(0, "end")
        detail = ""
        if preset["src_serial"] or preset["dst_serial"]:
            detail = "\nSerials: {} \u2192 {}".format(
                preset["src_serial"] or src, preset["dst_serial"] or dst)
        else:
            detail = "\nSaved by ID only (devices not visible in " \
                     "motoc show right now)."
        messagebox.showinfo("Preset saved",
                            "Saved preset '{}'.{}".format(name, detail))

    def _id_row(self, wrap, kind, ids, var):
        """One ID row: a horizontally scrolling button strip on the
        left, and a fixed type-an-ID field plus Add button on the
        right. Scaling: no matter how many buttons are added, the
        strip scrolls instead of pushing the entry off screen or
        stretching the window, and the entry never scrolls away.
        Returns (buttons_by_id, entry, canvas)."""
        outer = tk.Frame(wrap, bg=BG)
        outer.pack(fill="x", pady=(0, 18))

        scroll_frame = tk.Frame(outer, bg=BG)
        scroll_frame.pack(side="left", fill="x", expand=True)
        canvas = tk.Canvas(scroll_frame, bg=BG, height=54,
                           highlightthickness=0)
        hscroll = tk.Scrollbar(scroll_frame, orient="horizontal",
                               command=canvas.xview)
        canvas.configure(xscrollcommand=hscroll.set)
        canvas.pack(fill="x", expand=True)
        hscroll.pack(fill="x")
        inner = tk.Frame(canvas, bg=BG)
        canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda _e, c=canvas: c.configure(
                       scrollregion=c.bbox("all")))

        btns = {}
        for i in ids:
            btns[i] = self._id_button(inner, kind, var, i)

        side = tk.Frame(outer, bg=BG)
        side.pack(side="right", anchor="n", padx=(16, 0))
        tk.Label(side, text="or type an ID:", font=(SANS, 11),
                 bg=BG, fg=DIM).pack(anchor="w")
        entry = tk.Entry(side, font=(SANS, 18), width=6, bg="#0a0a12",
                         fg=ACCENT, insertbackground=ACCENT, relief="flat",
                         highlightthickness=1, highlightbackground="#33334a",
                         highlightcolor=ACCENT)
        entry.pack()
        entry.bind("<KeyRelease>", lambda _e: self.update_go())
        entry.bind("<Return>", lambda _e: self.go())
        add_b = tk.Button(side, text="Add as button", font=(SANS, 11),
                          command=lambda k=kind: self.add_custom(k))
        style_button(add_b, bg=ACCENT_BG, fg=ACCENT)
        add_b.pack(fill="x", pady=(6, 0))
        # v3.3: Delete as button, under Add as button. It deletes the
        # typed ID, or the currently selected button when the field
        # is empty, and only for buttons you added yourself. The
        # tooltip is the joke: "Delete as button" sounds dumb, so
        # hovering for 3 seconds says so.
        del_b = tk.Button(side, text="Delete as button", font=(SANS, 11),
                          command=lambda k=kind: self.delete_custom(k))
        style_button(del_b, bg="#2a1218", fg=FG)
        del_b.pack(fill="x", pady=(6, 0))
        if not hasattr(self, "_delete_tips"):
            self._delete_tips = {}
        self._delete_tips[kind] = HoverTip(del_b, "lol", delay_ms=3000)
        # Keep the inner frame lookup by kind for add_custom.
        if kind == "src":
            self._src_inner = inner
        else:
            self._dst_inner = inner
        return btns, entry, canvas

    def _id_button(self, parent, kind, var, value):
        """One ID button, wired to pick() for its row."""
        if kind == "src":
            cmd = lambda v=value: self.pick(self.src_var, self.src_btns,
                                            self.src_entry, v)
        else:
            cmd = lambda v=value: self.pick(self.dst_var, self.dst_btns,
                                            self.dst_entry, v)
        b = tk.Button(parent, text=value, font=(SANS, 18, "bold"),
                      width=4, pady=6, command=cmd)
        style_button(b)
        b.pack(side="left", padx=(0, 12))
        return b

    def add_custom(self, kind):
        """Turn the typed ID into a real button, to the left in the
        scrolling strip, and save it so it survives a restart."""
        if kind == "src":
            var, btns, entry, ids, inner, canvas = (
                self.src_var, self.src_btns, self.src_entry, self.src_ids,
                self._src_inner, self.src_canvas)
            defaults, key = self.SRC_IDS, "custom_src_ids"
        else:
            var, btns, entry, ids, inner, canvas = (
                self.dst_var, self.dst_btns, self.dst_entry, self.dst_ids,
                self._dst_inner, self.dst_canvas)
            defaults, key = self.DST_IDS, "custom_dst_ids"
        value = entry.get().strip()
        if not re.fullmatch(r"\d+", value):
            messagebox.showerror("Invalid device ID",
                                 "Type a number first, then Add as button.")
            return
        if value not in btns:
            btns[value] = self._id_button(inner, kind, var, value)
            ids.append(value)
            extra = [i for i in ids if i not in defaults]
            self.app._save_config(**{key: extra})
        # Select the new (or already existing) button, clear the field.
        self.pick(var, btns, entry, value)
        # Scroll the strip so the new button is visible.
        canvas.update_idletasks()
        canvas.xview_moveto(1.0)

    def delete_custom(self, kind):
        """Delete a button you added yourself.

        Target is the typed ID when the field has one, otherwise
        the currently selected button. Built-in buttons (1, 2 and
        3-7) cannot be deleted, and deleting is reversible just by
        typing the number again and hitting Add as button, so
        there is no confirm dialog in the way.
        """
        if kind == "src":
            var, btns, entry, ids = (
                self.src_var, self.src_btns, self.src_entry, self.src_ids)
            defaults, key = self.SRC_IDS, "custom_src_ids"
        else:
            var, btns, entry, ids = (
                self.dst_var, self.dst_btns, self.dst_entry, self.dst_ids)
            defaults, key = self.DST_IDS, "custom_dst_ids"
        typed = entry.get().strip()
        target = typed if typed else var.get()
        if not re.fullmatch(r"\d+", target):
            messagebox.showerror("Invalid device ID",
                                 "Type a number, or pick a button first, "
                                 "then Delete as button.")
            return
        if target in defaults:
            messagebox.showinfo(
                "Built-in button",
                "Button {} is built in, so it can't be deleted.\n\n"
                "Only buttons you added with Add as button can be "
                "deleted.".format(target))
            return
        if target not in btns:
            messagebox.showinfo(
                "No such button",
                "There is no {} button to delete.".format(target))
            return
        btns[target].destroy()
        del btns[target]
        ids.remove(target)
        extra = [i for i in ids if i not in defaults]
        self.app._save_config(**{key: extra})
        entry.delete(0, "end")
        if var.get() == target and ids:
            var.set(ids[0])
        self.update_go()

    def pick(self, var, btns, entry, value):
        var.set(value)
        entry.delete(0, "end")
        self.update_go()

    def effective(self, var, entry):
        """Custom field wins over the buttons when it has text."""
        custom = entry.get().strip()
        return custom if custom else var.get()

    def update_go(self):
        for i, b in self.src_btns.items():
            sel = not self.src_entry.get().strip() and self.src_var.get() == i
            b._current_bg = ACCENT_BG if sel else CARD
            b.configure(bg=b._current_bg,
                        fg=ACCENT if sel else FG)
        for i, b in self.dst_btns.items():
            sel = not self.dst_entry.get().strip() and self.dst_var.get() == i
            b._current_bg = ACCENT_BG if sel else CARD
            b.configure(bg=b._current_bg,
                        fg=ACCENT if sel else FG)

    def go(self):
        src = self.effective(self.src_var, self.src_entry)
        dst = self.effective(self.dst_var, self.dst_entry)
        if not re.fullmatch(r"\d+", src) or not re.fullmatch(r"\d+", dst):
            messagebox.showerror("Invalid device ID",
                                 "Device IDs must be numbers.\n"
                                 "Pick a button or type a custom ID.")
            return
        if src == dst:
            messagebox.showerror("Invalid device ID",
                                 "Source and target must be different "
                                 "devices.")
            return
        # A manual run never inherits an aborted calibrate-all queue.
        self.app.clear_queue()
        self.app.src, self.app.dst = src, dst
        self.app.remember_ids()
        self.app.show("ReadyScreen")


class PresetsScreen(Screen):
    """Saved serial presets, with single and calibrate-all runs."""

    def __init__(self, parent, app):
        super().__init__(parent, app)
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(expand=True, fill="both", padx=48, pady=28)
        tk.Label(wrap, text="Presets", font=(SANS, 26, "bold"),
                 bg=BG, fg=FG).pack(anchor="w", pady=(0, 6))
        tk.Label(wrap, text="Pairs saved by tracker serial, resolved to "
                            "today's IDs at run time.",
                 font=(SANS, 12), bg=BG, fg=DIM).pack(anchor="w",
                                                     pady=(0, 18))
        all_b = tk.Button(wrap, text="Calibrate all presets",
                          font=(SANS, 16, "bold"), pady=10,
                          command=self.calibrate_all)
        style_button(all_b, bg=ACCENT_BG, fg=ACCENT)
        all_b.pack(fill="x", pady=(0, 18))
        self.body = tk.Frame(wrap, bg=BG)
        self.body.pack(fill="both", expand=True)
        back = tk.Button(wrap, text="\u2190 Back", font=(SANS, 13),
                         command=lambda: self.app.show("MenuScreen"))
        style_button(back)
        back.pack(pady=(12, 0))

    def on_show(self):
        self.render()

    def render(self):
        for w in self.body.winfo_children():
            w.destroy()
        presets = [p for p in (self.app._load_config().get("presets", [])
                               or []) if isinstance(p, dict)]
        if not presets:
            tk.Label(self.body, text="No presets yet.\n\nPick IDs on the "
                                    "Custom screen and hit Save preset.",
                     font=(SANS, 14), bg=BG, fg=DIM,
                     justify="left").pack(anchor="w")
            return
        for preset in presets:
            row = tk.Frame(self.body, bg=CARD)
            row.pack(fill="x", pady=6)
            desc = "{}: {} \u2192 {}".format(
                preset.get("name", "?"),
                preset.get("src_serial") or preset.get("src_id", "?"),
                preset.get("dst_serial") or preset.get("dst_id", "?"))
            tk.Label(row, text=desc, font=(SANS, 14), bg=CARD, fg=FG,
                     anchor="w").pack(side="left", padx=16, pady=10,
                                      fill="x", expand=True)
            cal = tk.Button(row, text="Calibrate", font=(SANS, 12),
                            command=lambda p=preset: self.calibrate_one(p))
            style_button(cal, bg=ACCENT_BG, fg=ACCENT)
            cal.pack(side="right", padx=(0, 8), pady=8)
            dele = tk.Button(row, text="Delete", font=(SANS, 12),
                             command=lambda p=preset: self.delete_preset(p))
            style_button(dele)
            dele.pack(side="right", padx=(0, 16), pady=8)

    def delete_preset(self, preset):
        cfg = self.app._load_config()
        presets = [p for p in (cfg.get("presets", []) or [])
                   if isinstance(p, dict) and p.get("name") != preset.get("name")]
        self.app._save_config(presets=presets)
        self.render()

    def calibrate_one(self, preset):
        src, dst, missing = resolve_preset(preset)
        if not missing and src == dst:
            missing = ["source and target are the same device"]
        if missing:
            messagebox.showerror(
                "Preset not ready",
                "Preset '{}' is missing: {}\n\nPower on those trackers "
                "and try again.".format(preset.get("name", "?"),
                                        ", ".join(missing)))
            return
        self.app.current_preset_name = preset.get("name", "")
        self.app.cal_queue = []
        self.app.cal_queue_pos = 0
        self.app.src, self.app.dst = src, dst
        self.app.remember_ids()
        self.app.show("ReadyScreen")

    def calibrate_all(self):
        presets = [p for p in (self.app._load_config().get("presets", [])
                               or []) if isinstance(p, dict)]
        if not presets:
            messagebox.showinfo("Calibrate all",
                                "No presets to run yet.")
            return
        # One motoc show snapshot for the whole queue, not one call
        # per preset, so a down service does not freeze the UI
        # repeatedly before the "not ready" dialog appears.
        origins = motoc_show_origins()
        resolved, problems = [], []
        for preset in presets:
            src, dst, missing = resolve_preset_from_origins(origins, preset)
            if not missing and src == dst:
                missing = ["source and target are the same device"]
            if missing:
                problems.append("{}: {}".format(preset.get("name", "?"),
                                                ", ".join(missing)))
            else:
                resolved.append((preset.get("name", ""), src, dst))
        if problems:
            messagebox.showerror(
                "Calibrate all stopped",
                "These presets are not ready:\n\n{}\n\nPower on those "
                "trackers and try again.".format("\n".join(problems)))
            return
        self.app.start_queue(resolved)


class PreflightScreen(Screen):
    """One screen of checks before a calibration run."""

    def __init__(self, parent, app):
        super().__init__(parent, app)
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(expand=True, fill="both", padx=48, pady=28)
        tk.Label(wrap, text="Pre-flight check", font=(SANS, 26, "bold"),
                 bg=BG, fg=FG).pack(anchor="w", pady=(0, 18))
        self.text = tk.Text(wrap, font=(MONO, 12), bg="#0a0a12",
                            fg="#c8c8e0", relief="flat",
                            highlightthickness=1,
                            highlightbackground="#33334a",
                            state="disabled")
        self.text.pack(fill="both", expand=True, pady=(0, 14))
        ref = tk.Button(wrap, text="\u21bb Re-check", font=(SANS, 14),
                        command=self.run_checks)
        style_button(ref, bg=ACCENT_BG, fg=ACCENT)
        ref.pack(fill="x", pady=(0, 12))
        back = tk.Button(wrap, text="\u2190 Back", font=(SANS, 13),
                         command=lambda: self.app.show("MenuScreen"))
        style_button(back)
        back.pack()
        self._q = queue.Queue()
        self._job = None
        self._generation = 0

    def on_show(self):
        self.run_checks()

    def on_hide(self):
        if self._job:
            try:
                self.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def _append(self, text):
        self.text.configure(state="normal")
        self.text.insert("end", text)
        self.text.see("end")
        self.text.configure(state="disabled")

    def run_checks(self):
        if self._job:
            try:
                self.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None
        # Drain stale results, then start a new generation. Without
        # this, a second Re-check could display the first run's
        # results, because _poll consumes the oldest queue item.
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        self._generation += 1
        generation = self._generation
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self._append("Checking...\n")
        threading.Thread(target=self._worker, args=(generation,),
                         daemon=True).start()
        self._poll()

    def _worker(self, generation):
        lines = []
        lines.append(("PASS" if shutil.which("motoc") else "FAIL",
                      "motoc on PATH"))
        origins = motoc_show_origins()
        if origins is None:
            lines.append(("FAIL", "Monado reachable (motoc show)"))
        else:
            total = sum(len(devs) for _o, devs in origins)
            lines.append(("PASS", "Monado reachable, {} device(s)".format(total)))
            for origin, devs in origins:
                for idx, serial, name in devs:
                    lines.append(("INFO", "  [{}] {} {}".format(
                        idx, serial, name or "")))
        found = find_service_pids()
        for name in SERVICE_PROCS:
            lines.append(("PASS" if name in found else "INFO",
                          "{} running".format(name) if name in found
                          else "{} not running".format(name)))
        batt = read_battery()
        if not batt:
            lines.append(("INFO", "Battery: no data (helper missing or "
                                  "trackers do not report it)"))
        else:
            for idx, (pct, charging) in sorted(
                    batt.items(),
                    key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0):
                state = "PASS" if charging or pct > 20 else "WARN"
                lines.append((state, "Battery [{}]: {}%{}".format(
                    idx, pct, " (charging)" if charging else "")))
        self._q.put((generation, lines))

    def _poll(self):
        try:
            generation, lines = self._q.get_nowait()
        except queue.Empty:
            self._job = self.after(150, self._poll)
            return
        if generation != self._generation:
            # Stale run: keep polling for the current generation.
            self._job = self.after(150, self._poll)
            return
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for status, text in lines:
            self.text.insert("end", "[{}] {}\n".format(status, text))
        self.text.configure(state="disabled")
        self._job = None


class HistoryScreen(Screen):
    """Past calibration runs, newest first."""

    def __init__(self, parent, app):
        super().__init__(parent, app)
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(expand=True, fill="both", padx=48, pady=28)
        tk.Label(wrap, text="Calibration history", font=(SANS, 26, "bold"),
                 bg=BG, fg=FG).pack(anchor="w", pady=(0, 18))
        self.text = tk.Text(wrap, font=(MONO, 11), bg="#0a0a12",
                            fg="#c8c8e0", relief="flat",
                            highlightthickness=1,
                            highlightbackground="#33334a",
                            state="disabled")
        self.text.pack(fill="both", expand=True, pady=(0, 14))
        clear_b = tk.Button(wrap, text="Clear history", font=(SANS, 13),
                            command=self.clear_history)
        style_button(clear_b)
        clear_b.pack(fill="x", pady=(0, 12))
        back = tk.Button(wrap, text="\u2190 Back", font=(SANS, 13),
                         command=lambda: self.app.show("MenuScreen"))
        style_button(back)
        back.pack()

    def on_show(self):
        self.render()

    def render(self):
        history = load_history()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        if not history:
            self.text.insert("end", "No calibration runs recorded yet.\n")
        else:
            for entry in reversed(history[-50:]):
                serials = ""
                if entry.get("src_serial") or entry.get("dst_serial"):
                    serials = "  ({} -> {})".format(
                        entry.get("src_serial") or "?",
                        entry.get("dst_serial") or "?")
                self.text.insert("end", "{}  {} -> {}  {}  {}{}\n".format(
                    entry.get("time", "?"), entry.get("src", "?"),
                    entry.get("dst", "?"),
                    "OK" if entry.get("ok") else "FAIL",
                    entry.get("preset") or "manual", serials))
        self.text.configure(state="disabled")

    def clear_history(self):
        if not messagebox.askyesno("Clear history",
                                   "Delete all recorded calibration runs?"):
            return
        try:
            os.remove(HISTORY_PATH)
        except OSError:
            pass
        self.render()


class ReadyScreen(Screen):
    def __init__(self, parent, app):
        super().__init__(parent, app)
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(expand=True, fill="both", padx=48, pady=28)

        tk.Label(wrap, text="Ready to calibrate?", font=(SANS, 26, "bold"),
                 bg=BG, fg=FG).pack(pady=(36, 10))
        self.summary = tk.Label(wrap, text="", font=(SANS, 18),
                                bg=BG, fg=ACCENT)
        self.summary.pack(pady=(0, 18))
        tk.Label(wrap, text="Hold the source device firmly against\n"
                            "the target tracker, then start moving.",
                 font=(SANS, 14), bg=BG, fg=FG, justify="center").pack(pady=(0, 30))

        start = tk.Button(wrap, text="Start calibration", font=(SANS, 20, "bold"),
                          pady=12, command=lambda: self.app.show("CountdownScreen"))
        style_button(start, bg=ACCENT_BG, fg=ACCENT)
        start.pack(fill="x", pady=(0, 12))
        back = tk.Button(wrap, text="\u2190 Back", font=(SANS, 13),
                         command=self.back)
        style_button(back)
        back.pack()

    def back(self):
        # Leaving Ready without starting abandons any queue that
        # led here, so a later manual run does not inherit it.
        self.app.clear_queue()
        self.app.show("MenuScreen")

    def on_show(self):
        self.summary.configure(
            text="Source {}  \u2192  Target {}".format(self.app.src, self.app.dst))


class CountdownScreen(Screen):
    def __init__(self, parent, app):
        super().__init__(parent, app)
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(expand=True, fill="both")
        tk.Label(wrap, text="Get into position", font=(SANS, 22),
                 bg=BG, fg=DIM).pack(pady=(0, 56))
        self.num = tk.Label(wrap, text="", font=(SANS, 130, "bold"),
                            bg=BG, fg=ACCENT)
        self.num.pack()
        cancel_b = tk.Button(wrap, text="Cancel", font=(SANS, 14),
                             command=self.cancel)
        style_button(cancel_b)
        cancel_b.pack(pady=(24, 0))
        self._job = None

    def cancel(self):
        self.app.clear_queue()
        self.app.show("MenuScreen")

    def on_show(self):
        self.count(3)

    def count(self, n):
        if self._job:
            self.after_cancel(self._job)
            self._job = None
        if n <= 0:
            self.num.configure(text="GO!")
            self._job = self.after(700, lambda: self.app.show("RunScreen"))
        else:
            self.num.configure(text=str(n))
            self._job = self.after(1000, lambda: self.count(n - 1))

    def on_hide(self):
        if self._job:
            self.after_cancel(self._job)
            self._job = None


class RunScreen(Screen):
    def __init__(self, parent, app):
        super().__init__(parent, app)
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(expand=True, fill="both", padx=48, pady=28)

        tk.Label(wrap, text="Calibrating\u2026", font=(SANS, 26, "bold"),
                 bg=BG, fg=FG).pack(anchor="w", pady=(0, 10))
        self.banner = tk.Label(wrap, text="", font=(SANS, 14),
                               bg=BG, fg=ACCENT)
        self.banner.pack(anchor="w", pady=(0, 10))

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Cal.Horizontal.TProgressbar", troughcolor=CARD,
                        background=ACCENT, bordercolor=BG, lightcolor=ACCENT,
                        darkcolor=ACCENT, thickness=14)
        self.bar = ttk.Progressbar(wrap, mode="indeterminate",
                                   style="Cal.Horizontal.TProgressbar")
        self.bar.pack(fill="x", pady=(0, 14))

        self.log = tk.Text(wrap, height=12, font=(MONO, 10),
                           bg="#0a0a12", fg="#c8c8e0", relief="flat",
                           highlightthickness=1, highlightbackground="#33334a",
                           state="disabled")
        self.log.pack(fill="both", expand=True, pady=(0, 14))
        tk.Label(wrap, text="Move slowly so both devices travel together.",
                 font=(SANS, 12), bg=BG, fg=DIM).pack()
        cancel_b = tk.Button(wrap, text="Cancel", font=(SANS, 13),
                             command=self.cancel)
        style_button(cancel_b)
        cancel_b.pack(pady=(14, 0))

        self.q = queue.Queue()
        self.proc = None
        self._poll_job = None
        self.cancelled = False

    def cancel(self):
        self.cancelled = True
        if self._poll_job:
            try:
                self.after_cancel(self._poll_job)
            except tk.TclError:
                pass
            self._poll_job = None
        self.bar.stop()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass
        self.app.clear_queue()
        self.app.show("MenuScreen")

    def on_show(self):
        self.cancelled = False
        # Drain any done item left by a cancelled previous run.
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        self.banner.configure(
            text="Source {}  \u2192  Target {}".format(self.app.src, self.app.dst))
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.bar.start(12)
        threading.Thread(target=self.worker, daemon=True).start()
        self._poll()

    def on_hide(self):
        self.bar.stop()
        if self._poll_job:
            try:
                self.after_cancel(self._poll_job)
            except tk.TclError:
                pass
            self._poll_job = None
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass

    def append(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def worker(self):
        # Capture serials at run start, in this worker thread, so
        # logging the run later does not need a synchronous motoc
        # show call on the main thread after calibration finishes.
        origins = motoc_show_origins()
        _by_serial, by_index = serial_maps(origins) if origins else ({}, {})
        self.app.run_src_serial = by_index.get(self.app.src, "")
        self.app.run_dst_serial = by_index.get(self.app.dst, "")
        try:
            with open(LOG_PATH, "w") as logf:
                self.proc = subprocess.Popen(
                    ["motoc", "calibrate", "--src", self.app.src,
                     "--dst", self.app.dst],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1)
                for line in self.proc.stdout:
                    logf.write(line)
                    logf.flush()
                    self.q.put(("line", line))
                self.proc.wait()
                self.q.put(("done", self.proc.returncode))
        except FileNotFoundError:
            self.q.put(("done", 127))
        except Exception as e:  # noqa: BLE001 - surfaced in the log pane
            self.q.put(("line", "error: {}\n".format(e)))
            self.q.put(("done", 1))

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "line":
                    self.append(payload)
                else:
                    if self.cancelled:
                        return
                    self.app.cal_ok = (payload == 0)
                    self.app.log_run(self.app.cal_ok)
                    self.app.show("ResultScreen")
                    return
        except queue.Empty:
            pass
        self._poll_job = self.after(150, self._poll)


class ResultScreen(Screen):
    def __init__(self, parent, app):
        super().__init__(parent, app)
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(expand=True, fill="both", padx=48)

        self.icon = tk.Label(wrap, text="", font=(SANS, 64), bg=BG)
        self.icon.pack(pady=(70, 6))
        self.title = tk.Label(wrap, text="", font=(SANS, 30, "bold"), bg=BG)
        self.title.pack(pady=(0, 10))
        self.detail = tk.Label(wrap, text="", font=(SANS, 14),
                               bg=BG, fg=DIM, justify="center",
                               wraplength=560)
        self.detail.pack(pady=(0, 28))

        again = tk.Button(wrap, text="\u21bb Calibrate again", font=(SANS, 17, "bold"),
                          pady=10, command=self.again)
        style_button(again, bg=ACCENT_BG, fg=ACCENT)
        again.pack(fill="x", pady=(0, 12))
        # v4 queue button: only packed when a calibrate-all queue is
        # active; otherwise it stays hidden and the old flow is kept.
        self.queue_b = tk.Button(wrap, text="", font=(SANS, 17, "bold"),
                                 pady=10, command=self.app.advance_queue)
        style_button(self.queue_b, bg=ACCENT_BG, fg=ACCENT)
        self.quit_b = tk.Button(wrap, text="Done", font=(SANS, 13),
                                command=self.app.destroy)
        style_button(self.quit_b)
        self.quit_b.pack()

    def again(self):
        self.app.clear_queue()
        self.app.show("MenuScreen")

    def on_show(self):
        queue = getattr(self.app, "cal_queue", [])
        pos = getattr(self.app, "cal_queue_pos", 0)
        if queue:
            if pos + 1 < len(queue):
                nxt = queue[pos + 1][0]
                self.queue_b.configure(
                    text="Next: {} ({}/{})  \u2192".format(nxt, pos + 2,
                                                           len(queue)))
            else:
                self.queue_b.configure(text="Queue complete \u2014 back to menu")
            self.quit_b.pack_forget()
            self.queue_b.pack(fill="x", pady=(0, 12))
            self.quit_b.pack()
        else:
            self.queue_b.pack_forget()
        if self.app.cal_ok:
            self.icon.configure(text="\u2713", fg=GOOD)
            self.title.configure(text="Calibration successful", fg=GOOD)
            self.detail.configure(text="Your trackers are aligned.\n"
                                       "Full log: {}".format(LOG_PATH))
        else:
            self.icon.configure(text="\u2717", fg=BAD)
            self.title.configure(text="Calibration failed", fg=BAD)
            self.detail.configure(text="Check the log and try again.\n"
                                       "Full log: {}".format(LOG_PATH))


# ------------------------------------------------------------ device panel
class DeviceSidebar(tk.Frame):
    """Right-hand panel listing live tracking devices via `motoc show`.

    Never crashes the app: every failure mode (motoc missing, service
    offline, no devices) is rendered as a friendly status message.
    """

    REFRESH_MS = 20000
    # v4.3 backoff: after BACKOFF_AFTER consecutive service
    # failures, the auto-refresh slows to BACKOFF_MS. It never
    # pauses completely, so it still notices when WiVRn comes
    # back. A successful poll schedules the next refresh at 20 s;
    # if a 60 s interval was already scheduled before recovery,
    # that one interval still runs, then the following one is 20 s.
    BACKOFF_MS = 60000
    BACKOFF_AFTER = 3
    VRC_REFRESH_MS = 300000  # status pages barely change; don't hammer it

    def __init__(self, parent, width=264):
        super().__init__(parent, bg=SIDEBAR_BG, width=width)
        self.pack_propagate(False)
        self.q = queue.Queue()
        self.vrc_q = queue.Queue()
        self.refreshing = False
        self._fail_count = 0
        self._width = width
        self._wrap_labels = []
        self._vrc_wrap_labels = []

        tk.Label(self, text="Device status", font=(SANS, 14, "bold"),
                 bg=SIDEBAR_BG, fg=FG, anchor="w").pack(fill="x", padx=16,
                                                       pady=(16, 2))
        statusrow = tk.Frame(self, bg=SIDEBAR_BG)
        statusrow.pack(fill="x", padx=16, pady=(0, 8))
        self.dot = tk.Label(statusrow, text="\u25cf", font=(SANS, 11),
                            bg=SIDEBAR_BG, fg=DIM)
        self.dot.pack(side="left")
        self.statustext = tk.Label(statusrow, text="Scanning\u2026", font=(SANS, 11),
                                   bg=SIDEBAR_BG, fg=DIM, anchor="w")
        self.statustext.pack(side="left", padx=(6, 0))

        # v4 silent new-device note: plain DIM text, never a flash,
        # never a sound, never a popup. First render sets the
        # baseline and shows nothing, so launch does not flag
        # every already-connected device as "new".
        self._prev_serials = None
        self.new_label = tk.Label(self, text="", font=(SANS, 11),
                                  bg=SIDEBAR_BG, fg=DIM, anchor="w",
                                  justify="left")
        self.new_label.pack(fill="x", padx=16, pady=(0, 4))

        tk.Frame(self, bg="#262633", height=1).pack(fill="x", padx=16, pady=(0, 8))

        self.body = tk.Frame(self, bg=SIDEBAR_BG)
        self.body.pack(fill="both", expand=True, padx=16)

        tk.Frame(self, bg="#262633", height=1).pack(fill="x", padx=16,
                                                   pady=(8, 0))
        self.vrc_frame = tk.Frame(self, bg=SIDEBAR_BG)
        self.vrc_frame.pack(fill="x", padx=16, pady=(8, 0))

        bottom = tk.Frame(self, bg=SIDEBAR_BG)
        bottom.pack(fill="x", padx=16, pady=16)
        ref = tk.Button(bottom, text="\u21bb Refresh", font=(SANS, 12, "bold"),
                        command=self.refresh)
        style_button(ref, bg=ACCENT_BG, fg=ACCENT)
        ref.pack(fill="x", pady=4)

        # v3 debug section: visible on every screen, since the sidebar
        # never leaves. Restart is stop-here / start-from-dashboard
        # (see SERVICE_PROCS notes above), never a guessed relaunch.
        tk.Frame(bottom, bg="#262633", height=1).pack(fill="x", pady=(10, 6))
        tk.Label(bottom, text="DEBUG", font=(SANS, 10, "bold"),
                 bg=SIDEBAR_BG, fg=DIM, anchor="w").pack(fill="x")
        for label, cmd in (
                ("Restart WiVRn / Monado", self.restart_services),
                ("Start WiVRn", self.start_wivrn),
                ("Check VR services", self.check_services),
                ("Open calibration log", self.open_calibration_log)):
            b = tk.Button(bottom, text=label, font=(SANS, 12),
                          command=cmd)
            style_button(b)
            b.pack(fill="x", pady=2)
        exit_b = tk.Button(bottom, text="Exit", font=(SANS, 12),
                           command=self.winfo_toplevel().destroy)
        style_button(exit_b)
        exit_b.pack(fill="x", pady=(8, 2))
        tk.Label(bottom, text="v{}".format(VERSION), font=(SANS, 9),
                 bg=SIDEBAR_BG, fg="#6a6a85", anchor="e").pack(fill="x")

        self._restart_poll_job = None
        self._auto_pending = False
        self._auto_job = None
        self.refresh()
        self._auto_pending = True
        self._auto_job = self.after(self.REFRESH_MS, self._auto_refresh)
        self._render_vrc_checking()
        self._vrc_refresh()

    # -- public -------------------------------------------------------
    def refresh(self):
        if self.refreshing:
            return
        self.refreshing = True
        self.set_status("Scanning\u2026", DIM)
        threading.Thread(target=self._worker, daemon=True).start()
        self.after(150, self._poll)

    # -- v3 debug -----------------------------------------------------
    def check_services(self):
        """Show which VR services are actually running right now."""
        messagebox.showinfo("VR services", "\n".join(service_status_lines()))

    def _auto_close_info(self, title, message, ms=2000):
        """Non-modal info popup that closes itself after `ms`.

        Unlike messagebox.showinfo, this does not grab the app, so
        the user can already click over to the dashboard while it
        is showing, and it never waits for OK. OK still closes it
        early. The after() close guards against the window already
        being gone, so closing the app during those 2 seconds is
        safe.
        """
        parent = self.winfo_toplevel()
        dialog = tk.Toplevel(parent)
        dialog.title(title)
        dialog.transient(parent)
        try:
            dialog.attributes("-topmost", True)
        except tk.TclError:
            pass
        tk.Label(dialog, text=message, font=(SANS, 12),
                 justify="left", wraplength=360).pack(padx=20, pady=(20, 10))

        def close():
            try:
                if dialog.winfo_exists():
                    dialog.destroy()
            except tk.TclError:
                pass

        ok = tk.Button(dialog, text="OK", font=(SANS, 12), command=close)
        ok.pack(pady=(0, 20))
        try:
            parent.update_idletasks()
            dialog.update_idletasks()
            x = parent.winfo_rootx() + max(0, (parent.winfo_width() - 400) // 2)
            y = parent.winfo_rooty() + max(0, (parent.winfo_height() - 200) // 2)
            dialog.geometry("+{}+{}".format(x, y))
        except tk.TclError:
            pass
        dialog.after(ms, close)
        return dialog

    def start_wivrn(self):
        """MANUAL ONLY: launch the WiVRn dashboard, on this click.

        Nothing else in this app calls this: no auto-start on
        launch, on refresh, on service unreachable, or on any
        error path. If a server is already running, say so and do
        nothing. If no dashboard binary is on PATH, say so and
        leave starting it to the app menu, instead of guessing
        a server launch that would fight the dashboard.
        """
        found = find_service_pids()
        if found:
            messagebox.showinfo(
                "Start WiVRn",
                "WiVRn / Monado is already running.\n\n"
                "No need to start it again. Hit Refresh if the "
                "device list looks stale.")
            return
        dashboard = shutil.which("wivrn-dashboard")
        if not dashboard:
            messagebox.showerror(
                "Start WiVRn",
                "Could not find wivrn-dashboard on PATH.\n\n"
                "Start WiVRn from your app menu instead, then "
                "hit Refresh here.")
            return
        try:
            subprocess.Popen([dashboard],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError as e:
            messagebox.showerror("Start WiVRn",
                                 "Could not launch the dashboard:\n{}".format(e))
            return
        # v4.1: this one auto-closes in 2 seconds, it does not wait
        # for OK. The already-running and not-found dialogs above
        # stay modal, because those need to be read and acted on.
        self._auto_close_info(
            "Start WiVRn",
            "WiVRn dashboard launched.\n\n"
            "Press Start in the dashboard, then hit Refresh here "
            "to re-check your devices.",
            ms=2000)
        self.refresh()

    def open_calibration_log(self):
        if not os.path.isfile(LOG_PATH):
            messagebox.showinfo("Calibration log",
                                "No calibration log yet.\n"
                                "Run a calibration first; the log lands at:\n"
                                "{}".format(LOG_PATH))
            return
        if not open_in_terminal("cat '{}'".format(LOG_PATH)):
            messagebox.showerror(
                "No terminal found",
                "Couldn't open a terminal for the log.\n\n"
                "Read it yourself:\n\n    cat {}".format(LOG_PATH))

    def restart_services(self):
        """Stop the WiVRn/Monado server; the user restarts it in the
        dashboard, since that is what spawned it in the first place."""
        found = find_service_pids()
        if not found:
            messagebox.showinfo(
                "Restart WiVRn / Monado",
                "No wivrn-server or monado-service process is running.\n\n"
                "Start WiVRn from its dashboard first, then try again.")
            return
        detail = ", ".join("{} (pid {})".format(n, ", ".join(p))
                           for n, p in found.items())
        if not messagebox.askyesno(
                "Restart WiVRn / Monado",
                "Stop this server?\n\n{}\n\n"
                "This will drop your VR session. Afterwards, start "
                "WiVRn again from its dashboard, then hit Refresh "
                "here.".format(detail)):
            return
        if self._restart_poll_job:
            try:
                self.after_cancel(self._restart_poll_job)
            except tk.TclError:
                pass
            self._restart_poll_job = None
        pids = [p for ps in found.values() for p in ps]
        self._restart_q = queue.Queue()
        threading.Thread(target=self._restart_worker, args=(pids,),
                         daemon=True).start()
        self._restart_poll_job = self.after(150, self._restart_poll)

    def _restart_worker(self, pids):
        survivors = stop_service_pids(pids)
        self._restart_q.put(survivors)

    def _restart_poll(self):
        try:
            survivors = self._restart_q.get_nowait()
        except queue.Empty:
            self._restart_poll_job = self.after(150, self._restart_poll)
            return
        self._restart_poll_job = None
        if survivors:
            messagebox.showwarning(
                "Restart WiVRn / Monado",
                "These processes are still running: pid {}.\n\n"
                "Check them before starting WiVRn again.".format(
                    ", ".join(survivors)))
        else:
            messagebox.showinfo(
                "Restart WiVRn / Monado",
                "Server stopped.\n\n"
                "Start WiVRn again from its dashboard, then hit "
                "Refresh here to re-check your devices.")
        self.refresh()

    # -- internals ----------------------------------------------------
    # v4.3 backoff: the interval is decided in _poll, AFTER the
    # result is known, not in _auto_refresh before the worker
    # returns. Otherwise the switch to 60s (and back to 20s) lags
    # by one cycle. There is ever one auto job: _schedule_next_auto
    # cancels the previously scheduled job (if any) and schedules
    # a fresh one with the current fail count, so a manual Refresh
    # updates the interval instead of creating a second chain.
    def _auto_refresh(self):
        if not self._auto_pending:
            return
        self._auto_pending = False
        self._auto_job = None
        self.refresh()

    def _schedule_next_auto(self):
        if self._auto_job:
            try:
                self.after_cancel(self._auto_job)
            except tk.TclError:
                pass
        self._auto_pending = True
        interval = self.BACKOFF_MS if self._fail_count >= self.BACKOFF_AFTER \
            else self.REFRESH_MS
        self._auto_job = self.after(interval, self._auto_refresh)

    def _render_vrc_checking(self):
        self._vrc_wrap_labels = []
        tk.Label(self.vrc_frame, text="VRCHAT API STATUS",
                 font=(SANS, 10, "bold"), bg=SIDEBAR_BG, fg=DIM,
                 anchor="w").pack(fill="x", pady=(0, 2))
        tk.Label(self.vrc_frame, text="Checking status.vrchat.com\u2026",
                 font=(SANS, 11), bg=SIDEBAR_BG, fg=DIM,
                 anchor="w").pack(fill="x")

    def _vrc_refresh(self):
        """VRC status has its own loop so it shows even when motoc is down."""
        threading.Thread(target=self._vrc_worker, daemon=True).start()
        self.after(150, self._vrc_poll)

    def _vrc_worker(self):
        self.vrc_q.put(read_vrc_status())

    def _vrc_poll(self):
        try:
            vrc = self.vrc_q.get_nowait()
        except queue.Empty:
            self.after(150, self._vrc_poll)
            return
        self._render_vrc(vrc)
        self.after(self.VRC_REFRESH_MS, self._vrc_refresh)

    def _worker(self):
        try:
            proc = subprocess.run(["motoc", "show"], capture_output=True,
                                  text=True, timeout=12)
        except FileNotFoundError:
            self.q.put(("error", "The 'motoc' command was not found."))
            return
        except subprocess.TimeoutExpired:
            self.q.put(("error", "'motoc show' timed out."))
            return
        except Exception as e:  # noqa: BLE001 - shown in the panel
            self.q.put(("error", str(e)))
            return
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip().splitlines()
            detail = err[-1].strip() if err else \
                "exited with code {}".format(proc.returncode)
            # clap's usage dump is noise; keep the one-line reason.
            if len(detail) > 120:
                detail = detail[:117] + "\u2026"
            self.q.put(("error", detail))
            return
        try:
            origins = parse_motoc_show(proc.stdout or "")
            self.q.put(("ok", (origins, read_battery())))
        except Exception as e:  # noqa: BLE001 - shown in the panel
            self.q.put(("error", "Could not parse device list: {}".format(e)))

    def _poll(self):
        try:
            kind, payload = self.q.get_nowait()
        except queue.Empty:
            self.after(150, self._poll)
            return
        self.refreshing = False
        if kind == "ok":
            self._fail_count = 0
            origins, battery = payload
            self.render(origins, battery)
        else:
            self._fail_count += 1
            self.show_error(payload)
        # Schedule the next auto refresh now, with the updated
        # fail count. Manual refreshes also land here, but
        # _schedule_next_auto refuses a duplicate chain.
        self._schedule_next_auto()

    def set_status(self, text, color):
        self.dot.configure(fg=color)
        self.statustext.configure(text=text, fg=color)

    def clear_body(self):
        for w in self.body.winfo_children():
            w.destroy()
        self._wrap_labels = []

    def set_width(self, w):
        """Resize the panel (from the drag sash) and re-wrap message text."""
        self._width = w
        self.configure(width=w)
        wrap = self._wrap_width()
        for lbl in self._wrap_labels:
            lbl.configure(wraplength=wrap)
        for lbl in self._vrc_wrap_labels:
            lbl.configure(wraplength=max(120, wrap - 24))

    def _wrap_width(self):
        w = self.body.winfo_width()
        if w > 60:  # laid out: wrap to what actually fits on screen
            return max(120, w - 16)
        return max(120, self._width - 48)

    def render(self, origins, battery=None):
        self.clear_body()
        battery = battery or {}
        # Silent new-device note, before any early return, so a
        # device that appears while the list was empty is noted too.
        serials = set()
        for _origin, devs in origins:
            for _idx, serial, _name in devs:
                serials.add(serial)
        total = sum(len(devs) for _, devs in origins)
        if total > 0:
            # Only update the baseline when devices are listed. A
            # 0-device render must not reset it to empty, or the
            # next reconnect would flag every existing device new.
            if self._prev_serials is None:
                self._prev_serials = serials
            else:
                new = sorted(serials - self._prev_serials)
                if new:
                    text = "New device: {}".format(", ".join(new))
                    if len(text) > 60:
                        text = text[:57] + "\u2026"
                    self.new_label.configure(text=text)
                self._prev_serials = serials
        if total == 0:
            self.set_status("No devices connected", DIM)
            msg = tk.Label(self.body,
                           text="Nothing connected right now.\n\n"
                                "Start WiVRn, connect your\ncontrollers / trackers,\n"
                                "then hit Refresh.",
                           font=(SANS, 12), bg=SIDEBAR_BG, fg=DIM,
                           justify="center", wraplength=self._wrap_width())
            msg.pack(pady=24)
            self._wrap_labels.append(msg)
            return
        word = "device" if total == 1 else "devices"
        self.set_status("{} {} connected".format(total, word), GOOD)
        for origin_name, devs in origins:
            tk.Label(self.body, text=origin_name.upper(), font=(SANS, 10, "bold"),
                     bg=SIDEBAR_BG, fg=DIM, anchor="w").pack(fill="x", pady=(10, 2))
            if not devs:
                tk.Label(self.body, text="(no devices)", font=(SANS, 11),
                         bg=SIDEBAR_BG, fg=DIM, anchor="w").pack(fill="x")
                continue
            for idx, serial, name in devs:
                label = name if name else serial
                row = tk.Frame(self.body, bg=SIDEBAR_BG)
                row.pack(fill="x", pady=2)
                tk.Label(row, text="[{}]".format(idx), font=(SANS, 12, "bold"),
                         bg=SIDEBAR_BG, fg=ACCENT).pack(side="left")
                batt = battery.get(idx)
                if batt is not None:
                    # packed before the expanding name label so the badge
                    # keeps its width when the panel is squeezed narrow
                    self._battery_badge(row, *batt).pack(side="left",
                                                         padx=(6, 0))
                tk.Label(row, text=label, font=(SANS, 12), bg=SIDEBAR_BG,
                         fg=FG, anchor="w").pack(side="left", fill="x",
                                                 expand=True, padx=(6, 0))
                if name:
                    tk.Label(self.body, text=serial, font=(MONO, 10),
                             bg=SIDEBAR_BG, fg=DIM, anchor="w").pack(fill="x",
                                                                     padx=(36, 0))

    def _render_vrc(self, vrc):
        """VRChat API status section; hidden gracefully when offline."""
        for w in self.vrc_frame.winfo_children():
            w.destroy()
        self._vrc_wrap_labels = []
        tk.Label(self.vrc_frame, text="VRCHAT API STATUS",
                 font=(SANS, 10, "bold"), bg=SIDEBAR_BG, fg=DIM,
                 anchor="w").pack(fill="x", pady=(0, 2))
        if not vrc:
            tk.Label(self.vrc_frame, text="Couldn't reach status.vrchat.com",
                     font=(SANS, 11), bg=SIDEBAR_BG, fg=DIM,
                     anchor="w").pack(fill="x")
            return
        dot_color, headline, comps = vrc
        vrc_wrap = max(120, self._wrap_width() - 24)  # room for the dot
        hrow = tk.Frame(self.vrc_frame, bg=SIDEBAR_BG)
        hrow.pack(fill="x", pady=(0, 4))
        tk.Label(hrow, text="\u25cf", font=(SANS, 11), bg=SIDEBAR_BG,
                 fg=dot_color).pack(side="left")
        hl = tk.Label(hrow, text=headline, font=(SANS, 11, "bold"),
                      bg=SIDEBAR_BG, fg=FG, anchor="w", justify="left",
                      wraplength=vrc_wrap)
        hl.pack(side="left", padx=(6, 0))
        self._vrc_wrap_labels.append(hl)
        for name, label, color in comps:
            row = tk.Frame(self.vrc_frame, bg=SIDEBAR_BG)
            row.pack(fill="x", pady=1)
            tk.Label(row, text="\u25cf", font=(SANS, 9), bg=SIDEBAR_BG,
                     fg=color).pack(side="left", anchor="n", pady=2)
            degraded = label != "Operational"
            text = name if not degraded else "{} \u2014 {}".format(name, label)
            nm = tk.Label(row, text=text, font=(SANS, 11), bg=SIDEBAR_BG,
                          fg=color if degraded else FG, anchor="w",
                          justify="left", wraplength=vrc_wrap)
            nm.pack(side="left", padx=(6, 0))
            self._vrc_wrap_labels.append(nm)

    @staticmethod
    def _battery_badge(parent, pct, charging):
        """Little battery icon + % — drawn, not emoji, so it always renders."""
        f = tk.Frame(parent, bg=SIDEBAR_BG)
        c = tk.Canvas(f, width=27, height=13, bg=SIDEBAR_BG,
                      highlightthickness=0)
        c.pack(side="left")
        c.create_rectangle(1, 2, 22, 11, outline="#6a6a85")
        c.create_rectangle(23, 5, 25, 8, fill="#6a6a85", outline="")
        if charging:
            col = "#7dd3fc"  # blue = charging
        elif pct > 40:
            col = "#4ade80"
        elif pct > 20:
            col = "#fbbf24"
        else:
            col = "#f87171"
        w = max(0, min(19, round(19 * pct / 100)))
        if w:
            c.create_rectangle(2, 3, 2 + w, 10, fill=col, outline="")
        tk.Label(f, text="{}%".format(pct), font=(SANS, 10),
                 bg=SIDEBAR_BG, fg=col).pack(side="left", padx=(3, 0))
        return f

    def show_error(self, detail):
        self.clear_body()
        self.set_status("Service unreachable", BAD)
        msg = tk.Label(self.body,
                       text="Couldn't reach the tracking service.\n\n"
                            "Is WiVRn / Monado running?\nHit Refresh to retry.",
                       font=(SANS, 12), bg=SIDEBAR_BG, fg=DIM,
                       justify="center", wraplength=self._wrap_width())
        msg.pack(pady=24)
        self._wrap_labels.append(msg)
        det = tk.Label(self.body, text=detail, font=(MONO, 9),
                       bg=SIDEBAR_BG, fg="#6a6a85", justify="center",
                       wraplength=self._wrap_width())
        det.pack()
        self._wrap_labels.append(det)
        if self._fail_count >= self.BACKOFF_AFTER:
            note = tk.Label(self.body,
                            text="Auto-refresh is now once a minute "
                                 "until the service comes back.\n"
                                 "Manual Refresh still works at any time.",
                            font=(SANS, 11), bg=SIDEBAR_BG, fg=DIM,
                            justify="center",
                            wraplength=self._wrap_width())
            note.pack(pady=(16, 0))
            self._wrap_labels.append(note)


# ------------------------------------------------------------------- app
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("WiVRn FBT Calibration")
        self.configure(bg=BG)
        self.geometry("980x620")
        self.minsize(800, 540)
        cfg = self._load_config()
        self.src = cfg.get("last_src", "1")
        self.dst = cfg.get("last_dst", "7")
        if not (isinstance(self.src, str) and self.src.isdigit()):
            self.src = "1"
        if not (isinstance(self.dst, str) and self.dst.isdigit()):
            self.dst = "7"
        self.cal_ok = False
        # v4 queue / preset state. A manual calibration leaves these
        # empty; a preset or calibrate-all run sets them, and the
        # result screen advances or finishes the queue.
        self.cal_queue = []
        self.cal_queue_pos = 0
        self.current_preset_name = ""
        self.run_src_serial = ""
        self.run_dst_serial = ""

        try:
            self.tk.call("tk", "windowingsystem")  # noqa: F841
        except tk.TclError:
            pass

        tk.Frame(self, bg=ACCENT, height=4).pack(fill="x")

        main = tk.Frame(self, bg=BG)
        main.pack(fill="both", expand=True)

        self.container = tk.Frame(main, bg=BG)
        self.container.pack(side="left", fill="both", expand=True)
        self.container.grid_rowconfigure(0, weight=1)
        self.container.grid_columnconfigure(0, weight=1)

        tk.Frame(main, bg="#23232f", width=1).pack(side="left", fill="y")

        self.sidebar = DeviceSidebar(main, width=self._load_sidebar_width())

        # Draggable sash: grab it to resize the device panel.
        self.sash = tk.Frame(main, bg="#23232f", width=8,
                             cursor="sb_h_double_arrow")
        self.sash.pack(side="left", fill="y")
        self.sidebar.pack(side="left", fill="y")
        self._drag = None
        self.sash.bind("<Button-1>", self._on_sash_press)
        self.sash.bind("<B1-Motion>", self._on_sash_drag)
        self.sash.bind("<ButtonRelease-1>", self._on_sash_release)
        self.sash.bind("<Enter>",
                       lambda _e: self.sash.configure(bg="#3a3a55"))
        self.sash.bind("<Leave>",
                       lambda _e: self.sash.configure(bg="#23232f"))

        self.screens = {}
        self.current = None
        for cls in (MenuScreen, PickScreen, PresetsScreen,
                    PreflightScreen, HistoryScreen, ReadyScreen,
                    CountdownScreen, RunScreen, ResultScreen):
            screen = cls(self.container, self)
            self.screens[cls.__name__] = screen
            screen.grid(row=0, column=0, sticky="nsew")
        self.show("MenuScreen")

        w, h = 980, 620
        # Placement, in order: where you dragged it last time, else
        # centred on the primary monitor, else the old whole-desktop
        # centring. Saved position wins even over the primary, so
        # if your "main" monitor is not the OS primary, drag it
        # once and it stays there from then on.
        saved_x, saved_y = cfg.get("window_x"), cfg.get("window_y")
        if isinstance(saved_x, int) and isinstance(saved_y, int):
            x, y = saved_x, saved_y
        else:
            primary = primary_monitor_geometry()
            if primary:
                px, py, pw, ph = primary
                x = px + max(0, (pw - w) // 2)
                y = py + max(0, (ph - h) // 2)
            else:
                x = (self.winfo_screenwidth() - w) // 2
                y = (self.winfo_screenheight() - h) // 2
        self.geometry("{}x{}+{}+{}".format(w, h, x, y))
        # v3.1: start expanded (maximised). The geometry above still
        # picks which monitor it maximises on, from the saved
        # position or the primary monitor, so the monitor fix stays.
        try:
            self.state("zoomed")
        except tk.TclError:
            try:
                self.attributes("-zoomed", True)
            except tk.TclError:
                pass
        # Fallback for window managers that ignore zoomed: after the
        # WM has had a chance, if we are still small, resize to the
        # primary monitor ourselves. On a normal KDE session zoomed
        # already worked, so this does nothing there.
        self.after(200, self._ensure_expanded)
        self.bind("<Destroy>", self._on_destroy)
        self.bind("<Destroy>", self._save_window_position, add="+")

    def _on_destroy(self, event):
        # A1: closing the app must stop child processes. on_hide
        # is not called on destroy, so terminate them here.
        if event.widget is not self:
            return
        try:
            menu = self.screens.get("MenuScreen")
            if menu is not None:
                menu.stop_monitor()
        except (tk.TclError, AttributeError):
            pass
        try:
            run = self.screens.get("RunScreen")
            if run is not None and run.proc is not None \
                    and run.proc.poll() is None:
                run.proc.terminate()
        except (tk.TclError, AttributeError, OSError):
            pass

    def _ensure_expanded(self):
        try:
            if self.state() == "zoomed":
                return
            if self.winfo_width() >= 1200:
                return
        except tk.TclError:
            return
        primary = primary_monitor_geometry()
        if primary:
            px, py, pw, ph = primary
            self.geometry("{}x{}+{}+{}".format(pw, ph, px, py))
        else:
            # C5: do not span the full virtual desktop when there
            # is no primary marker; keep the previous geometry.
            pass

    def _save_window_position(self, event):
        # Only the root window's destroy, not every child widget's.
        if event.widget is not self:
            return
        try:
            self._save_config(window_x=self.winfo_x(),
                              window_y=self.winfo_y())
        except (tk.TclError, OSError):
            pass

    def show(self, name):
        if self.current and hasattr(self.current, "on_hide"):
            self.current.on_hide()
        self.current = self.screens[name]
        self.current.on_show()
        self.current.tkraise()

    # -- v4 queue and history ---------------------------------------
    def clear_queue(self):
        self.cal_queue = []
        self.cal_queue_pos = 0
        self.current_preset_name = ""

    def start_queue(self, resolved):
        """resolved: [(preset_name, src_id, dst_id), ...], already checked."""
        self.cal_queue = list(resolved)
        self.cal_queue_pos = 0
        self.current_preset_name, self.src, self.dst = resolved[0]
        self.remember_ids()
        self.show("ReadyScreen")

    def advance_queue(self):
        """From the result screen: next pair, or finish the queue."""
        if not self.cal_queue:
            self.show("MenuScreen")
            return
        if self.cal_queue_pos + 1 < len(self.cal_queue):
            self.cal_queue_pos += 1
            self.current_preset_name, self.src, self.dst = \
                self.cal_queue[self.cal_queue_pos]
            self.remember_ids()
            self.show("ReadyScreen")
        else:
            self.clear_queue()
            self.show("MenuScreen")

    def log_run(self, ok):
        """Append this run to history.json, using the serials
        captured at run start, with no new motoc show call."""
        append_history({
            "time": time.strftime("%Y-%m-%d %H:%M"),
            "src": self.src,
            "dst": self.dst,
            "src_serial": getattr(self, "run_src_serial", ""),
            "dst_serial": getattr(self, "run_dst_serial", ""),
            "ok": bool(ok),
            "preset": self.current_preset_name,
        })

    # -- sidebar resizing -------------------------------------------
    def _on_sash_press(self, event):
        self._drag = (event.x_root, self.sidebar.winfo_width())

    def _on_sash_drag(self, event):
        if not self._drag:
            return
        start_x, start_w = self._drag
        new_w = start_w + (start_x - event.x_root)
        new_w = max(200, min(new_w, int(self.winfo_width() * 0.6)))
        self.sidebar.set_width(new_w)

    def _on_sash_release(self, _event):
        self._drag = None
        self._save_sidebar_width()

    @staticmethod
    def _load_config():
        try:
            with open(CONFIG_PATH) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
            _backup_corrupt(CONFIG_PATH)
            return {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            if os.path.isfile(CONFIG_PATH):
                _backup_corrupt(CONFIG_PATH)
            return {}

    def _save_config(self, **updates):
        # Merge into the existing config; the old sidebar save replaced
        # the whole file, which would have wiped anything else stored.
        # Writes are atomic, so a crash mid-write cannot leave a
        # half-written config that the next load then overwrites.
        cfg = self._load_config()
        cfg.update(updates)
        try:
            _write_json_atomic(CONFIG_PATH, cfg)
        except OSError:
            pass

    @staticmethod
    def _load_sidebar_width():
        try:
            w = int(App._load_config().get("sidebar_width", 264))
        except (TypeError, ValueError):
            w = 264
        return max(200, min(w, 600))

    def _save_sidebar_width(self):
        self._save_config(sidebar_width=self.sidebar.winfo_width())

    def remember_ids(self):
        """Persist the current source/target IDs as the next defaults."""
        self._save_config(last_src=self.src, last_dst=self.dst)


def main():
    if not shutil.which("motoc"):
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Missing dependency",
            "'motoc' was not found on your PATH.\n"
            "Install it, then relaunch the calibrator.")
        root.destroy()
        sys.exit(1)
    try:
        App().mainloop()
    except Exception:
        import traceback
        err = traceback.format_exc()
        try:
            with open(ERROR_LOG_PATH, "w") as f:
                f.write(err)
        except OSError:
            pass
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("FBT Calibrator crashed",
                                 err[-2000:] +
                                 "\n\nFull log: {}".format(ERROR_LOG_PATH))
            root.destroy()
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
