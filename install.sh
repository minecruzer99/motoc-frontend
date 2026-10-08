#!/bin/sh
# Install the FBT Calibrator for the current user (no sudo).
#
#   ./install.sh
#
# Installs into ~/.local/share/fbt-calibrator, puts a `fbt-calibrator`
# command in ~/.local/bin, and generates a desktop launcher with the
# correct paths for YOUR machine (the .desktop file in this repo is a
# template with a placeholder path).
set -eu

SRC_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
INSTALL_DIR="$DATA_HOME/fbt-calibrator"
BIN_DIR="$HOME/.local/bin"
APPS_DIR="$DATA_HOME/applications"
ICON_DIR="$DATA_HOME/icons/hicolor/256x256/apps"

fail() { echo "install: $*" >&2; exit 1; }

# --- prerequisites ---------------------------------------------------
command -v python3 >/dev/null 2>&1 || fail "python3 not found on PATH."

if ! python3 -c "import tkinter" >/dev/null 2>&1; then
    cat >&2 <<'EOF'
install: python3-tkinter is missing (the GUI needs it). Install it with
your distro's package manager, then re-run ./install.sh:

  Arch:           sudo pacman -S tk
  Debian/Ubuntu:  sudo apt install python3-tk
  Fedora:         sudo dnf install python3-tkinter
EOF
    exit 1
fi

if ! command -v motoc >/dev/null 2>&1; then
    echo "install: WARNING: 'motoc' was not found on PATH." >&2
    echo "         The calibrator drives motoc (it ships with the WiVRn" >&2
    echo "         tooling) and will refuse to start without it." >&2
fi

# --- install ---------------------------------------------------------
mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$APPS_DIR" "$ICON_DIR"

cp "$SRC_DIR/fbt-calibrator.py" "$INSTALL_DIR/fbt-calibrator.py"
cp "$SRC_DIR/fbt-calibrator.png" "$ICON_DIR/fbt-calibrator.png"

if [ -f "$SRC_DIR/fbt-battery" ]; then
    cp "$SRC_DIR/fbt-battery" "$INSTALL_DIR/fbt-battery"
    chmod +x "$INSTALL_DIR/fbt-battery"
else
    echo "install: note: no prebuilt fbt-battery helper found — battery"
    echo "         badges will be hidden. To enable them, build the helper"
    echo "         in battery-helper/ (see README.md)."
fi

# Command-line launcher
cat > "$BIN_DIR/fbt-calibrator" <<EOF
#!/bin/sh
exec python3 "$INSTALL_DIR/fbt-calibrator.py" "\$@"
EOF
chmod +x "$BIN_DIR/fbt-calibrator"

# Desktop launcher, generated from the template with real paths
sed "s|__APP_DIR__|$INSTALL_DIR|g" "$SRC_DIR/Calibrate-FBT.desktop.in" \
    > "$APPS_DIR/calibrate-fbt.desktop"
chmod +x "$APPS_DIR/calibrate-fbt.desktop"
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
fi

echo "Installed to $INSTALL_DIR"
echo "Run it from your app menu (\"Calibrate FBT\") or with: fbt-calibrator"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "note: $BIN_DIR is not on your PATH; add it to run 'fbt-calibrator' directly." ;;
esac
