#!/bin/bash
# Removes ClipBoard for Linux Mint. Pass --purge to also delete pinned items.
gdbus call --session --dest io.github.tahmidsikder.ClipBoard --object-path /io/github/tahmidsikder/ClipBoard \
    --method org.gtk.Actions.Activate quit '[]' '{}' >/dev/null 2>&1 || true
pkill -f '^python3 [^ ]*/(\.local/bin/clipboard-linux|clipboard-linux\.py)( |$)' 2>/dev/null || true

python3 - << 'EOF' || true
from gi.repository import Gio
if Gio.SettingsSchemaSource.get_default().lookup("org.cinnamon.desktop.keybindings", True):
    schema = "org.cinnamon.desktop.keybindings.custom-keybinding"
    base = "/org/cinnamon/desktop/keybindings/custom-keybindings/"
    settings = Gio.Settings.new("org.cinnamon.desktop.keybindings")
    ids = list(settings.get_strv("custom-list"))
    for i in list(ids):
        kb = Gio.Settings.new_with_path(schema, base + i + "/")
        if kb.get_string("name") == "ClipBoard":
            for key in ("name", "command", "binding"):
                kb.reset(key)
            ids.remove(i)
    settings.set_strv("custom-list", ids)
    Gio.Settings.sync()
EOF

rm -f "$HOME/.local/bin/clipboard-linux" \
      "$HOME/.local/share/applications/clipboard-linux.desktop" \
      "$HOME/.config/autostart/clipboard-linux.desktop" \
      "$HOME/.local/share/icons/hicolor/256x256/apps/clipboard-linux.png"
rm -rf "$HOME/.local/share/clipboard-linux/app" "$HOME/.cache/clipboard-linux"
if [ "$1" = "--purge" ]; then
    rm -rf "$HOME/.local/share/clipboard-linux"
    echo "ClipBoard removed (including pinned items)."
else
    echo "ClipBoard removed. Pinned items kept in ~/.local/share/clipboard-linux (use --purge to delete)."
fi
