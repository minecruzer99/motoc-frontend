#!/bin/sh
# Sort the FBT Calibrator folder. Run this, no sudo.
#
#   sh sort-fbt-folder.sh --dry-run     # preview only, moves nothing
#   sh sort-fbt-folder.sh               # shows the plan, asks y/N
#   sh sort-fbt-folder.sh --yes         # sort without asking
#
# Put this script in the FBT Calibrator folder itself (the one with
# Calibrate-FBT.desktop in it) and run it from there. It only sorts
# loose files in that one folder, it never recurses into subfolders,
# and it never moves a folder.
#
# Where things go:
#   Python Scripts/   all *.py files, including the version switcher
#                     and every calibrator version, plus fbt-battery
#                     if present (the calibrator only looks for it
#                     next to its own .py, so it must travel with
#                     the scripts, not to Battery Helper/)
#   Battery Helper/   main.rs, Cargo.lock, Cargo.toml,
#                     README-battery-helper.md
#   Docs/             any loose *.md files except README.md (see below)
#
# Deliberately left in the root, never moved:
#   Calibrate-FBT.desktop and its .in / .old / .bak copies (launchers)
#   setup-switcher.sh (so you can re-run setup after a new version)
#   install.sh (legacy v2.5 installer, see its header warning)
#   LICENSE and README.md (so README links to LICENSE still work)
#   this script itself
#   __pycache__/ directories (Python regenerates these, ignore them)
#
# Anything else loose in the root is left in place and listed at
# the end, so the script never hides a file it does not recognise.
#
# Safety:
#   - If a file with the same name is already in the destination,
#     the root copy is NOT overwritten. It is skipped and listed,
#     so you can compare and delete one by hand.
#   - Every move is written to a sort-log-<timestamp>.txt in the
#     root, so you can see exactly what went where.
#   - Running it twice is safe: the second run finds nothing left
#     to move and does nothing, it never nests folders inside
#     themselves.
#   - At the end, if setup-switcher.sh is still in the root, it is
#     run for you. That repairs Calibrate-FBT.desktop to point at
#     the switcher wherever it ended up (root or Python Scripts),
#     and lists the versions the switcher will offer. Without that
#     step, moving the switcher would break launching again.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DRY_RUN=0
YES=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --yes) YES=1 ;;
        *) echo "sort-fbt-folder: unknown option: $arg" >&2
           echo "usage: sh sort-fbt-folder.sh [--dry-run] [--yes]" >&2
           exit 1 ;;
    esac
done

# Destination for a loose file, or empty for "leave in root".
dest_for() {
    name=$(basename -- "$1")
    case "$name" in
        *.py|fbt-battery) echo "Python Scripts" ;;
        main.rs|Cargo.lock|Cargo.toml|README-battery-helper.md)
            echo "Battery Helper" ;;
        *.md) echo "Docs" ;;
        *) echo "" ;;
    esac
}

# Files that must stay in the root no matter what they match above.
stay_in_root() {
    name=$(basename -- "$1")
    case "$name" in
        sort-fbt-folder.sh|setup-switcher.sh|install.sh|LICENSE|README.md|Calibrate-FBT.desktop|*.desktop|*.desktop.in|*.desktop.bak.*|*.old.desktop)
            return 0 ;;
        *) return 1 ;;
    esac
}

# Collect the plan first, so --dry-run and the prompt show the
# same thing the real run will do.
plan=""
skipped=""
left=""
for f in "$SCRIPT_DIR"/*; do
    [ -e "$f" ] || continue
    [ -d "$f" ] && continue
    if stay_in_root "$f"; then
        continue
    fi
    dest=$(dest_for "$f")
    if [ -z "$dest" ]; then
        left="$left
  $(basename -- "$f") (left in root: not a recognised type)"
        continue
    fi
    if [ -e "$SCRIPT_DIR/$dest/$(basename -- "$f")" ]; then
        skipped="$skipped
  $(basename -- "$f") -> $dest/ (skipped: already there, not overwritten)"
        continue
    fi
    plan="$plan
  $(basename -- "$f") -> $dest/"
done

if [ -z "$plan" ]; then
    echo "sort-fbt-folder: nothing to sort."
    [ -n "$skipped" ] && echo "Skipped:$skipped"
    [ -n "$left" ] && echo "Left in root:$left"
    exit 0
fi

echo "sort-fbt-folder: plan for $SCRIPT_DIR"
[ -n "$plan" ] && echo "Move:$plan"
[ -n "$skipped" ] && echo "Skipped:$skipped"
[ -n "$left" ] && echo "Left in root:$left"

if [ "$DRY_RUN" -eq 1 ]; then
    echo ""
    echo "Dry run only, nothing was moved."
    exit 0
fi

if [ "$YES" -ne 1 ]; then
    if [ -t 0 ]; then
        printf "Sort these files? [y/N] "
        read -r answer
        case "$answer" in
            y|Y|yes|YES) ;;
            *) echo "sort-fbt-folder: cancelled, nothing was moved."
               exit 0 ;;
        esac
    else
        echo ""
        echo "Not a terminal, so nothing was moved."
        echo "Run again with --yes to sort, or --dry-run to preview."
        exit 0
    fi
fi

LOG="$SCRIPT_DIR/sort-log-$(date +%Y%m%d%H%M%S).txt"
echo "sort-fbt-folder log for $SCRIPT_DIR, $(date)" > "$LOG"
for f in "$SCRIPT_DIR"/*; do
    [ -e "$f" ] || continue
    [ -d "$f" ] && continue
    if stay_in_root "$f"; then
        continue
    fi
    dest=$(dest_for "$f")
    [ -n "$dest" ] || continue
    if [ -e "$SCRIPT_DIR/$dest/$(basename -- "$f")" ]; then
        continue
    fi
    mkdir -p "$SCRIPT_DIR/$dest"
    mv -- "$f" "$SCRIPT_DIR/$dest/"
    echo "$(basename -- "$f") -> $dest/" >> "$LOG"
    echo "moved: $(basename -- "$f") -> $dest/"
done
echo "Log written to: $LOG"
echo ""

if [ -f "$SCRIPT_DIR/setup-switcher.sh" ]; then
    echo "Repairing the launcher with setup-switcher.sh..."
    sh "$SCRIPT_DIR/setup-switcher.sh"
else
    echo "setup-switcher.sh was not found in the root."
    echo "If you moved the switcher, run setup-switcher.sh by hand"
    echo "afterwards, or the .desktop will point at the old location."
fi
