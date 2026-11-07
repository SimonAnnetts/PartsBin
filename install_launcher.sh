#!/bin/sh
# Install (or remove) a desktop launcher for PartsBin on Linux.
#   ./install_launcher.sh              add PartsBin to your applications menu
#   ./install_launcher.sh --uninstall  remove it again
# The launcher records this folder's absolute path, so run it again if you move PartsBin.
set -eu

APP_DIR=$(cd "$(dirname "$0")" && pwd)
DEST_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DEST="$DEST_DIR/partsbin.desktop"

if [ "${1:-}" = "--uninstall" ]; then
    rm -f "$DEST"
    echo "Removed $DEST"
    exit 0
fi

# prefer a virtual environment in this folder (see README), else the system python3
if [ -x "$APP_DIR/.venv/bin/python" ]; then
    PYTHON="$APP_DIR/.venv/bin/python"
else
    PYTHON=$(command -v python3) || { echo "python3 not found" >&2; exit 1; }
fi

mkdir -p "$DEST_DIR"
cat > "$DEST" <<DESKTOP
[Desktop Entry]
Type=Application
Name=PartsBin
Comment=Catalogue of electronic components in stock
Exec="$PYTHON" "$APP_DIR/partsbin.py"
Path=$APP_DIR
Icon=$APP_DIR/icon.png
Terminal=false
Categories=Electronics;Utility;
StartupWMClass=Partsbin
DESKTOP
chmod 644 "$DEST"

command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$DEST_DIR" || true
echo "Installed $DEST"
echo "PartsBin should now appear in your applications menu (you may need to log out and in)."
