#!/bin/bash
# Installs ClipBoard for Linux Mint for the current user (no sudo needed).
set -e
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
APP_DIR="$HOME/.local/share/clipboard-linux/app"
BIN="$HOME/.local/bin/clipboard-linux"
DBUS_CALL="gdbus call --session --dest io.github.tahmidsikder.ClipBoard --object-path /io/github/tahmidsikder/ClipBoard --method org.gtk.Actions.Activate"
SHORTCUT="${1:-<Super>v}"

echo "============================================="
echo "   Installing ClipBoard for Linux Mint..."
echo "============================================="

# 1. Check dependencies (preinstalled on Linux Mint)
if ! python3 -c "import gi; gi.require_version('Gtk', '3.0'); from gi.repository import Gtk" 2>/dev/null; then
    echo "Missing GTK for Python. Install it with:"
    echo "   sudo apt install python3-gi gir1.2-gtk-3.0"
    exit 1
fi

# 2. Stop any currently running instance
$DBUS_CALL quit '[]' '{}' >/dev/null 2>&1 || true
pkill -f '^python3 [^ ]*/(\.local/bin/clipboard-linux|clipboard-linux\.py)( |$)' 2>/dev/null || true

# 3. Copy the app
echo "-> Copying ClipBoard to $APP_DIR..."
mkdir -p "$APP_DIR" "$HOME/.local/bin" "$HOME/.local/share/applications" \
         "$HOME/.local/share/icons/hicolor/256x256/apps" "$HOME/.config/autostart"
install -m 755 "$DIR/clipboard-linux.py" "$APP_DIR/clipboard-linux.py"
install -m 644 "$DIR/clipboard-linux.png" "$APP_DIR/clipboard-linux.png"
install -m 644 "$DIR/clipboard-linux.png" "$HOME/.local/share/icons/hicolor/256x256/apps/clipboard-linux.png"
install -m 755 "$DIR/uninstall.sh" "$APP_DIR/uninstall.sh"
ln -sf "$APP_DIR/clipboard-linux.py" "$BIN"

# 4. Menu entry
cat > "$HOME/.local/share/applications/clipboard-linux.desktop" << EOF
[Desktop Entry]
Type=Application
Name=ClipBoard
Comment=Clipboard history and emoji picker (Super+V)
Exec=$BIN --toggle
Icon=clipboard-linux
Terminal=false
Categories=Utility;
Keywords=clipboard;history;paste;emoji;
EOF

# 5. Start automatically at login
echo "-> Enabling automatic start at login..."
cat > "$HOME/.config/autostart/clipboard-linux.desktop" << EOF
[Desktop Entry]
Type=Application
Name=ClipBoard
Comment=Clipboard history (Super+V)
Exec=$BIN
Icon=clipboard-linux
Terminal=false
X-GNOME-Autostart-enabled=true
NoDisplay=false
EOF
update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
gtk-update-icon-cache -q "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true

# 6. Global keyboard shortcut (Cinnamon)
echo "-> Setting keyboard shortcut $SHORTCUT..."
TOGGLE_CMD="sh -c \"$DBUS_CALL toggle '[]' '{}' >/dev/null 2>&1 || '$BIN' --toggle\""
python3 - "$TOGGLE_CMD" "$SHORTCUT" << 'EOF' || SHORTCUT_FAILED=1
import sys
from gi.repository import Gio
command, binding = sys.argv[1], sys.argv[2]
if Gio.SettingsSchemaSource.get_default().lookup("org.cinnamon.desktop.keybindings", True) is None:
    sys.exit(1)
schema = "org.cinnamon.desktop.keybindings.custom-keybinding"
base = "/org/cinnamon/desktop/keybindings/custom-keybindings/"
settings = Gio.Settings.new("org.cinnamon.desktop.keybindings")
ids = list(settings.get_strv("custom-list"))
slot = next((i for i in ids if Gio.Settings.new_with_path(schema, base + i + "/").get_string("name") == "ClipBoard"), None)
if slot is None:
    n = 0
    while f"custom{n}" in ids:
        n += 1
    slot = f"custom{n}"
kb = Gio.Settings.new_with_path(schema, base + slot + "/")
kb.set_string("name", "ClipBoard")
kb.set_string("command", command)
kb.set_strv("binding", [binding])
if slot not in ids:
    ids.append(slot)
    settings.set_strv("custom-list", ids)
Gio.Settings.sync()
EOF

# 7. Launch application
echo "-> Launching ClipBoard..."
setsid "$BIN" >/dev/null 2>&1 < /dev/null &

echo "============================================="
echo " ✅ ClipBoard installed successfully!"
echo " 🚀 Auto-start at login is ENABLED!"
echo ""
if [ -n "$SHORTCUT_FAILED" ]; then
    echo " ⚠ Could not set the shortcut automatically (not Cinnamon?)."
    echo "   Add one in your Keyboard settings with this command:"
    echo "   $BIN --toggle"
else
    echo " Keyboard shortcut:  Super + V   (the Windows key + V)"
    echo " Change it any time: System Settings > Keyboard > Shortcuts > Custom Shortcuts"
fi
echo "============================================="
