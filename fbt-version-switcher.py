#!/usr/bin/env python3
"""FBT Calibrator version switcher.

Point a .desktop launcher at this file once, instead of at a single
calibrator version. On launch it scans the folder it lives in for
calibrator builds and offers them as a menu:

    fbt-calibrator.py        -> whatever VERSION it declares (v2.5)
    fbt-calibrator-v3.2.py   -> v3.2
    fbt-calibrator-v3.3.py   -> whatever comes next

Naming rule, from v3.2 on: a release file is always named
fbt-calibrator-v<its VERSION>.py, so the filename and the version
it runs can never drift apart again, as they did when v3.1 and
v3.2 were both shipped inside a file still called v3.

No renaming, no .desktop edits when a new version lands: just drop
the new .py in the same folder as this switcher and it shows up
here automatically. Duplicate rule, stated explicitly: the root
folder is searched first, then "Python Scripts", so if the same
correctly-named version exists in both places the root copy wins;
the menu now shows the folder for each version, so that choice is
visible instead of silent. It also searches a "Python Scripts" subfolder
next to it, so sorting the .py files into that folder (as in
Nov 2026, when moving them out of the switcher's folder made the
menu come up empty) does not break launching: versions in either
place show up, duplicates are still collapsed, and the picked
version is launched by its full path, wherever it lives.
The switcher itself never calibrates anything;
it just launches the version you pick and exits.

A build is recognised by filename (fbt-calibrator*.py, excluding
this switcher) plus a VERSION = "x.y" line inside it. Files without
a VERSION line still show up, labelled by filename, so a build is
never hidden just because its version marker is missing. If two
files declare the same version (for example a stale v3 copy next
to the correctly named v3.2 copy), only one menu entry appears,
for the file whose name matches that version, so the menu cannot
be cluttered with duplicates.
"""

import glob
import json
import os
import re
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox

BG = "#101018"
CARD = "#1a1a28"
CARD_HOVER = "#26263a"
FG = "#f0f0fa"
DIM = "#9a9ab5"
ACCENT = "#00e5ff"
ACCENT_BG = "#0a2a33"
SANS = "DejaVu Sans"

VERSION_RE = re.compile(r'^VERSION\s*=\s*["\']([^"\']+)["\']', re.M)


FILENAME_VERSION_RE = re.compile(r"^fbt-calibrator-v([\d.]+)\.py$")


def search_folders(folder):
    """[folders to scan for versions], own folder first.

    The "Python Scripts" subfolder is searched second, so versions
    sorted into it are found even when the switcher itself stays
    in the parent folder. If the switcher itself lives inside
    "Python Scripts", its own folder is that subfolder and the
    second entry is a duplicate, which is dropped.
    """
    folders = [folder, os.path.join(folder, "Python Scripts")]
    seen = set()
    out = []
    for f in folders:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def discover_versions(folder):
    """[(version_label, path)] sorted newest version first.

    Searches `folder` and its "Python Scripts" subfolder (see
    search_folders). Version labels come from the VERSION line in
    each file; a file without one falls back to its filename so
    it is still pickable. Duplicates are collapsed: if several
    files declare the same version, keep only the one whose
    filename matches that version (fbt-calibrator-v3.2.py for
    v3.2), falling back to the first file found when none
    matches, so a stale copy cannot add a second, identical menu
    entry.
    """
    found = []
    for search_folder in search_folders(folder):
        for path in glob.glob(os.path.join(search_folder,
                                           "fbt-calibrator*.py")):
            name = os.path.basename(path)
            if name == os.path.basename(__file__):
                continue
            try:
                with open(path) as f:
                    text = f.read()
            except OSError:
                continue
            m = VERSION_RE.search(text)
            label = "v{}".format(m.group(1)) if m else name
            found.append((label, path))
    by_label = {}
    for label, path in found:
        name = os.path.basename(path)
        fm = FILENAME_VERSION_RE.match(name)
        matches = bool(fm and "v{}".format(fm.group(1)) == label)
        current = by_label.get(label)
        if current is None or (matches and not current[2]):
            by_label[label] = (label, path, matches)
    found = [(label, path) for label, path, _ in by_label.values()]

    def sort_key(item):
        label, path = item
        nums = re.findall(r"\d+", label)
        try:
            return tuple(int(n) for n in nums)
        except ValueError:
            return ()
    found.sort(key=lambda item: (sort_key(item), os.path.basename(item[1])),
               reverse=True)
    return found


CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".config",
                           "fbt-calibrator", "config.json")


def _load_config():
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_config(**updates):
    cfg = _load_config()
    cfg.update(updates)
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f)
    except OSError:
        pass


def primary_monitor_geometry():
    """(x, y, w, h) of the primary monitor from xrandr, or None.

    Same reason as in the calibrator: Tk's screen size is the whole
    virtual desktop, so whole-desktop centring lands on the middle
    monitor of a multi-monitor setup, not the primary one.
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
        if m and m.group(1) == "*":
            return (int(m.group(5)), int(m.group(6)),
                    int(m.group(3)), int(m.group(4)))
    return None


def switcher_height(count, screen_height):
    """Window height for `count` version cards.

    The old window was a fixed 520x340, but a card is about 82px
    tall and the title, Cancel button, and note need about 240px
    on top of that, so two versions already need about 404px and
    three need about 486px. A fixed 340px window clipped Cancel
    and the last card. Height grows with the count, capped at 85%
    of the screen, and beyond that cap the version list scrolls
    internally instead of clipping.
    """
    try:
        screen_height = int(screen_height)
    except (TypeError, ValueError):
        screen_height = 900
    cap = max(340, int(screen_height * 0.85))
    return max(340, min(240 + 82 * count, cap))


def launch(path, root=None):
    """Start the chosen calibrator and let this switcher exit.

    Destroying the root (instead of sys.exit) lets the position-save
    binding run, so a dragged switcher window is remembered too.
    """
    subprocess.Popen([sys.executable, path])
    if root is not None:
        root.destroy()
    else:
        sys.exit(0)


def main():
    folder = os.path.dirname(os.path.abspath(__file__))
    versions = discover_versions(folder)
    if not versions:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "No calibrator found",
            "No fbt-calibrator*.py file was found in:\n\n{}\n\n"
            "or in its \"Python Scripts\" subfolder.\n\n"
            "Put your calibrator versions in one of those two "
            "places.".format(folder))
        root.destroy()
        return 1

    root = tk.Tk()
    root.title("FBT Calibrator - pick a version")
    root.configure(bg=BG)
    root.minsize(420, 260)

    wrap = tk.Frame(root, bg=BG)
    wrap.pack(expand=True, fill="both", padx=40, pady=28)

    tk.Label(wrap, text="FBT Calibrator", font=(SANS, 26, "bold"),
             bg=BG, fg=FG, anchor="w").pack(fill="x")
    tk.Label(wrap, text="Pick a version to launch", font=(SANS, 13),
             bg=BG, fg=DIM, anchor="w").pack(fill="x", pady=(0, 20))

    # Version list in a scrollable canvas, with Cancel and the note
    # in a fixed bottom frame, so Cancel can never be clipped off
    # the bottom again no matter how many versions are listed.
    bottom = tk.Frame(wrap, bg=BG)
    bottom.pack(side="bottom", fill="x")
    cancel = tk.Button(bottom, text="Cancel", font=(SANS, 13), pady=6,
                       bg=CARD, fg=FG, activebackground=CARD_HOVER,
                       activeforeground=ACCENT, relief="flat", bd=0,
                       highlightthickness=1, highlightbackground="#33334a",
                       cursor="hand2", command=root.destroy)
    cancel.pack(fill="x", pady=(16, 0))
    tk.Label(bottom, text="New versions appear here automatically when "
                          "dropped in this folder, or in its "
                          "\"Python Scripts\" folder.",
             font=(SANS, 10), bg=BG, fg=DIM, anchor="w",
             wraplength=440, justify="left").pack(fill="x", pady=(12, 0))

    list_frame = tk.Frame(wrap, bg=BG)
    list_frame.pack(fill="both", expand=True)
    canvas = tk.Canvas(list_frame, bg=BG, highlightthickness=0)
    vscroll = tk.Scrollbar(list_frame, orient="vertical",
                           command=canvas.yview)
    canvas.configure(yscrollcommand=vscroll.set)
    canvas.pack(side="left", fill="both", expand=True)
    vscroll.pack(side="right", fill="y")
    inner = tk.Frame(canvas, bg=BG)
    window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
    inner.bind("<Configure>",
               lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>",
                lambda e: canvas.itemconfig(window_id, width=e.width))

    def _scroll(event):
        delta = getattr(event, "delta", 0)
        if delta:
            canvas.yview_scroll(-1 if delta > 0 else 1, "units")
        elif event.num == 4:
            canvas.yview_scroll(-1, "units")
        elif event.num == 5:
            canvas.yview_scroll(1, "units")
    for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        canvas.bind_all(seq, _scroll)

    for label, path in versions:
        # B12: show where the version lives, so a root copy vs a
        # "Python Scripts" copy is visible, not a silent root win.
        try:
            display = os.path.relpath(path, folder)
        except ValueError:
            display = os.path.basename(path)
        name = display
        f = tk.Frame(inner, bg=CARD)
        f.pack(fill="x", pady=6)
        b = tk.Button(f, text=label, font=(SANS, 16, "bold"), anchor="w",
                      padx=20, pady=8, bg=CARD, fg=FG,
                      activebackground=CARD_HOVER, activeforeground=ACCENT,
                      relief="flat", bd=0, highlightthickness=1,
                      highlightbackground="#33334a", cursor="hand2",
                      command=lambda p=path: launch(p, root))
        b.pack(fill="x")
        b.bind("<Enter>", lambda e, w=b: w.configure(bg=CARD_HOVER))
        b.bind("<Leave>", lambda e, w=b: w.configure(bg=CARD))
        tk.Label(f, text=name, font=(SANS, 11), bg=CARD, fg=DIM,
                 anchor="w", padx=22).pack(fill="x", pady=(0, 10))

    # Height scales with the version count; placement below uses it.
    _primary_early = primary_monitor_geometry()
    _screen_h = _primary_early[3] if _primary_early else \
        root.winfo_screenheight()
    w, h = 520, switcher_height(len(versions), _screen_h)
    # Same placement rule as the calibrator: last dragged position
    # first, else centred on the primary monitor, else whole-desktop
    # centring. Drag it once to your main monitor and it stays there.
    cfg = _load_config()
    saved_x, saved_y = cfg.get("switcher_x"), cfg.get("switcher_y")
    try:
        if isinstance(saved_x, int) and isinstance(saved_y, int):
            x, y = saved_x, saved_y
        else:
            primary = primary_monitor_geometry()
            if primary:
                px, py, pw, ph = primary
                x = px + max(0, (pw - w) // 2)
                y = py + max(0, (ph - h) // 2)
            else:
                x = (root.winfo_screenwidth() - w) // 2
                y = (root.winfo_screenheight() - h) // 2
        root.geometry("{}x{}+{}+{}".format(w, h, x, y))
    except tk.TclError:
        pass

    def _save_position(event):
        if event.widget is not root:
            return
        try:
            _save_config(switcher_x=root.winfo_x(),
                         switcher_y=root.winfo_y())
        except (tk.TclError, OSError):
            pass
    root.bind("<Destroy>", _save_position)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
