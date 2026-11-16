#!/bin/sh
# FBT Calibrator version-switcher setup. Run this ONCE, no sudo.
#
#   1. Put this script in the folder holding your .desktop files
#      (for example, the "FBT Calibrator V3.0" folder on your
#      Desktop). The switcher and the calibrator .py files may
#      live there, or in a "Python Scripts" subfolder of it.
#   2. Run it, no executable permission needed:
#        sh setup-switcher.sh
#   3. Double-click the Calibrate-FBT.desktop it creates, or launch
#      "Calibrate FBT" from your app menu. A menu lets you pick a
#      version, so you never rename files or edit a .desktop again.
#
# What it does:
#   - Finds the switcher in this folder or in "Python Scripts".
#   - Checks at least one calibrator .py is in one of those places.
#   - Backs up any existing Calibrate-FBT.desktop (once, to
#     Calibrate-FBT.old.desktop, or a timestamped copy if that
#     already exists).
#   - Writes a fresh Calibrate-FBT.desktop whose Exec line points
#     at the switcher, with the folder path quoted so spaces in
#     "FBT Calibrator V3.0" and in "Python Scripts" do not break it.
#   - Sets the launcher icon to fbt-calibrator.png, when that file
#     is in this folder. It is copied to ~/.local/share/icons/ (a
#     path with no spaces), and the .desktop points at that copy,
#     so the icon survives sorting and folder renames. Without the
#     PNG, it falls back to the old theme name "fbt-calibrator".
#   - Makes the switcher and the .desktop executable.
#   - Copies the .desktop into ~/.local/share/applications so it
#     also shows up in the app menu.
# It does not touch, rename, or delete any calibrator version.
# Re-run it after sorting files into "Python Scripts": that is
# what repairs a launcher left pointing at a moved switcher.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -f "$SCRIPT_DIR/fbt-version-switcher.py" ]; then
    SWITCHER="$SCRIPT_DIR/fbt-version-switcher.py"
elif [ -f "$SCRIPT_DIR/Python Scripts/fbt-version-switcher.py" ]; then
    SWITCHER="$SCRIPT_DIR/Python Scripts/fbt-version-switcher.py"
else
    SWITCHER=""
fi
DESKTOP_FILE="$SCRIPT_DIR/Calibrate-FBT.desktop"
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons"
if [ -f "$SCRIPT_DIR/fbt-calibrator.png" ]; then
    ICON_SRC="$SCRIPT_DIR/fbt-calibrator.png"
elif [ -f "$SCRIPT_DIR/Python Scripts/fbt-calibrator.png" ]; then
    ICON_SRC="$SCRIPT_DIR/Python Scripts/fbt-calibrator.png"
else
    ICON_SRC=""
fi
if [ -n "$ICON_SRC" ]; then
    mkdir -p "$ICONS_DIR"
    cp "$ICON_SRC" "$ICONS_DIR/fbt-calibrator.png"
    ICON="$ICONS_DIR/fbt-calibrator.png"
else
    ICON="fbt-calibrator"
fi

fail() { echo "setup-switcher: $*" >&2; exit 1; }

[ -n "$SWITCHER" ] || fail "fbt-version-switcher.py is not in $SCRIPT_DIR
or in $SCRIPT_DIR/Python Scripts
Put the switcher in one of those places and try again."

# At least one calibrator build must be in one of those places,
# or the menu would be empty.
found=""
for f in "$SCRIPT_DIR"/fbt-calibrator*.py \
         "$SCRIPT_DIR/Python Scripts"/fbt-calibrator*.py; do
    [ -f "$f" ] && found="$found $f"
done
[ -n "$found" ] || fail "no fbt-calibrator*.py found in $SCRIPT_DIR
or in $SCRIPT_DIR/Python Scripts"

command -v python3 >/dev/null 2>&1 || fail "python3 was not found."
python3 -m py_compile "$SWITCHER" || fail "the switcher did not compile."

chmod +x "$SWITCHER"

if [ -f "$DESKTOP_FILE" ]; then
    if [ -f "$SCRIPT_DIR/Calibrate-FBT.old.desktop" ]; then
        mv "$DESKTOP_FILE" \
            "$SCRIPT_DIR/Calibrate-FBT.desktop.bak.$(date +%Y%m%d%H%M%S%N)"
    else
        mv "$DESKTOP_FILE" "$SCRIPT_DIR/Calibrate-FBT.old.desktop"
    fi
    echo "setup-switcher: backed up the old launcher."
fi

# Quoted Exec: this is the part that breaks by hand when the folder
# name has spaces. The quotes around the full path are deliberate.
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Name=Calibrate FBT
Comment=WiVRn full-body tracking calibration (pick a version)
Exec=python3 "$SWITCHER"
Icon=$ICON
Type=Application
Terminal=false
StartupNotify=false
Categories=Game;Utility;
EOF
chmod +x "$DESKTOP_FILE"

mkdir -p "$APPS_DIR"
cp "$DESKTOP_FILE" "$APPS_DIR/calibrate-fbt.desktop"
chmod +x "$APPS_DIR/calibrate-fbt.desktop"
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
fi

echo "setup-switcher: done."
echo "  switcher:  $SWITCHER"
echo "  launcher:  $DESKTOP_FILE"
echo "  icon:      $ICON"
echo "  app menu:  $APPS_DIR/calibrate-fbt.desktop"
echo ""
echo "Versions the switcher will offer:"
python3 - "$SCRIPT_DIR" "$SWITCHER" <<'PYEOF'
import importlib.util, os, sys
folder, switcher = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("sw", switcher)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
for label, path in mod.discover_versions(folder):
    print("  {}  ({})".format(label, os.path.basename(path)))
PYEOF
echo ""
echo "Now double-click Calibrate-FBT.desktop in this folder."
