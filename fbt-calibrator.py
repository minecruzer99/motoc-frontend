#!/usr/bin/env python3
"""WiVRn FBT Calibration GUI.

A dark, VR-friendly front end for `motoc calibrate`.

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
VERSION = "2.5"
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
    b.configure(bg=bg, fg=fg, activebackground=CARD_HOVER,
                activeforeground=ACCENT, relief="flat", bd=0,
                highlightthickness=1, highlightbackground="#33334a",
                cursor="hand2")
    b.bind("<Enter>", lambda _e: b.configure(bg=CARD_HOVER))
    b.bind("<Leave>", lambda _e: b.configure(bg=bg))


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
                  "Left controller (1)  \u2192  Tracker (7)", self.quick)
        self.card(wrap, "Custom Calibrate",
                  "Pick your own source & target device IDs", self.custom)
        self.card(wrap, "Find Device IDs",
                  "Open motoc monitor to list connected devices", self.find_ids)

        tk.Label(wrap, text="powered by motoc", font=(SANS, 11),
                 bg=BG, fg=DIM).pack(side="bottom", pady=(20, 0))

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
        if not open_in_terminal("motoc monitor"):
            messagebox.showerror(
                "No terminal found",
                "Couldn't find a terminal emulator to run "
                "'motoc monitor' in.\n\n"
                "Run it yourself in any terminal:\n\n"
                "    motoc monitor")


class PickScreen(Screen):
    SRC_IDS = ["1", "2"]
    DST_IDS = ["3", "4", "5", "6", "7"]

    def __init__(self, parent, app):
        super().__init__(parent, app)
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(expand=True, fill="both", padx=48, pady=28)

        tk.Label(wrap, text="Custom calibration", font=(SANS, 26, "bold"),
                 bg=BG, fg=FG).pack(anchor="w", pady=(0, 18))

        tk.Label(wrap, text="Source device ID", font=(SANS, 15),
                 bg=BG, fg=DIM).pack(anchor="w", pady=(0, 8))
        row1 = tk.Frame(wrap, bg=BG)
        row1.pack(anchor="w", pady=(0, 18))
        self.src_var = tk.StringVar(
            value=app.src if app.src in self.SRC_IDS else self.SRC_IDS[0])
        for i in self.SRC_IDS:
            b = tk.Button(row1, text=i, font=(SANS, 18, "bold"),
                          width=4, pady=6,
                          command=lambda v=i: self.pick(self.src_var,
                                                        self.src_btns,
                                                        self.src_entry, v))
            style_button(b)
            b.pack(side="left", padx=(0, 12))
        self.src_btns = {i: c for i, c in zip(self.SRC_IDS, row1.winfo_children())}
        self.src_entry = self._custom_entry(row1)
        if app.src not in self.SRC_IDS:
            self.src_entry.insert(0, app.src)

        tk.Label(wrap, text="Target device ID", font=(SANS, 15),
                 bg=BG, fg=DIM).pack(anchor="w", pady=(0, 8))
        row2 = tk.Frame(wrap, bg=BG)
        row2.pack(anchor="w", pady=(0, 24))
        self.dst_var = tk.StringVar(
            value=app.dst if app.dst in self.DST_IDS else self.DST_IDS[0])
        for i in self.DST_IDS:
            b = tk.Button(row2, text=i, font=(SANS, 18, "bold"),
                          width=4, pady=6,
                          command=lambda v=i: self.pick(self.dst_var,
                                                        self.dst_btns,
                                                        self.dst_entry, v))
            style_button(b)
            b.pack(side="left", padx=(0, 12))
        self.dst_btns = {i: c for i, c in zip(self.DST_IDS, row2.winfo_children())}
        self.dst_entry = self._custom_entry(row2)
        if app.dst not in self.DST_IDS:
            self.dst_entry.insert(0, app.dst)

        go = tk.Button(wrap, text="Continue  \u2192", font=(SANS, 18, "bold"),
                       pady=10, command=self.go)
        style_button(go, bg=ACCENT_BG, fg=ACCENT)
        go.pack(fill="x", pady=(0, 12))
        back = tk.Button(wrap, text="\u2190 Back", font=(SANS, 13),
                         command=lambda: self.app.show("MenuScreen"))
        style_button(back)
        back.pack()
        self.update_go()

    def _custom_entry(self, row):
        """A small 'type any ID' field tacked onto the end of a button row."""
        frame = tk.Frame(row, bg=BG)
        frame.pack(side="left")
        tk.Label(frame, text="or type an ID:", font=(SANS, 11),
                 bg=BG, fg=DIM).pack(anchor="w")
        e = tk.Entry(frame, font=(SANS, 18), width=6, bg="#0a0a12",
                     fg=ACCENT, insertbackground=ACCENT, relief="flat",
                     highlightthickness=1, highlightbackground="#33334a",
                     highlightcolor=ACCENT)
        e.pack()
        e.bind("<KeyRelease>", lambda _e: self.update_go())
        e.bind("<Return>", lambda _e: self.go())
        return e

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
            b.configure(bg=ACCENT_BG if sel else CARD,
                        fg=ACCENT if sel else FG)
        for i, b in self.dst_btns.items():
            sel = not self.dst_entry.get().strip() and self.dst_var.get() == i
            b.configure(bg=ACCENT_BG if sel else CARD,
                        fg=ACCENT if sel else FG)

    def go(self):
        src = self.effective(self.src_var, self.src_entry)
        dst = self.effective(self.dst_var, self.dst_entry)
        if not re.fullmatch(r"\d+", src) or not re.fullmatch(r"\d+", dst):
            messagebox.showerror("Invalid device ID",
                                 "Device IDs must be numbers.\n"
                                 "Pick a button or type a custom ID.")
            return
        self.app.src, self.app.dst = src, dst
        self.app.remember_ids()
        self.app.show("ReadyScreen")


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
                         command=lambda: self.app.show("MenuScreen"))
        style_button(back)
        back.pack()

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
        self._job = None

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

        self.q = queue.Queue()
        self.proc = None
        self._poll_job = None

    def on_show(self):
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
            self.after_cancel(self._poll_job)
            self._poll_job = None
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def append(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def worker(self):
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
                    self.app.cal_ok = (payload == 0)
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
                          pady=10, command=lambda: self.app.show("MenuScreen"))
        style_button(again, bg=ACCENT_BG, fg=ACCENT)
        again.pack(fill="x", pady=(0, 12))
        quit_b = tk.Button(wrap, text="Done", font=(SANS, 13),
                           command=self.app.destroy)
        style_button(quit_b)
        quit_b.pack()

    def on_show(self):
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
    VRC_REFRESH_MS = 300000  # status pages barely change; don't hammer it

    def __init__(self, parent, width=264):
        super().__init__(parent, bg=SIDEBAR_BG, width=width)
        self.pack_propagate(False)
        self.q = queue.Queue()
        self.vrc_q = queue.Queue()
        self.refreshing = False
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
        tk.Label(bottom, text="v{}".format(VERSION), font=(SANS, 9),
                 bg=SIDEBAR_BG, fg="#6a6a85", anchor="e").pack(fill="x")

        self.refresh()
        self.after(self.REFRESH_MS, self._auto_refresh)
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

    # -- internals ----------------------------------------------------
    def _auto_refresh(self):
        self.refresh()
        self.after(self.REFRESH_MS, self._auto_refresh)

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
            origins, battery = payload
            self.render(origins, battery)
        else:
            self.show_error(payload)

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
        total = sum(len(devs) for _, devs in origins)
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
        for cls in (MenuScreen, PickScreen, ReadyScreen,
                    CountdownScreen, RunScreen, ResultScreen):
            screen = cls(self.container, self)
            self.screens[cls.__name__] = screen
            screen.grid(row=0, column=0, sticky="nsew")
        self.show("MenuScreen")

        w, h = 980, 620
        x = (self.winfo_screenwidth() - w) // 2
        y = (self.winfo_screenheight() - h) // 2
        self.geometry("{}x{}+{}+{}".format(w, h, x, y))

    def show(self, name):
        if self.current and hasattr(self.current, "on_hide"):
            self.current.on_hide()
        self.current = self.screens[name]
        self.current.on_show()
        self.current.tkraise()

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
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_config(self, **updates):
        # Merge into the existing config; the old sidebar save replaced
        # the whole file, which would have wiped anything else stored.
        cfg = self._load_config()
        cfg.update(updates)
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(CONFIG_PATH, "w") as f:
                json.dump(cfg, f)
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
