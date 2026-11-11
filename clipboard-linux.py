#!/usr/bin/env python3
"""ClipBoard for Linux Mint — a Windows 11 (Win+V) style clipboard history.

Runs as a small background app. Toggle the popup with:
    clipboard-linux --toggle
(the installer binds this to Super+V).
"""
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkX11", "3.0")
from gi.repository import Gdk, GdkPixbuf, GdkX11, Gio, GLib, Gtk, Pango  # noqa: E402

try:
    gi.require_version("XApp", "1.0")
    from gi.repository import XApp
except (ValueError, ImportError):
    XApp = None

APP_ID = "io.github.tahmidsikder.ClipBoard"
APP_NAME = "ClipBoard"
DATA_DIR = os.path.join(GLib.get_user_data_dir(), "clipboard-linux")
CACHE_DIR = os.path.join(GLib.get_user_cache_dir(), "clipboard-linux")
PINS_FILE = os.path.join(DATA_DIR, "pins.json")
IMAGE_DIR = os.path.join(DATA_DIR, "images")
ICON_FILE = os.path.join(os.path.dirname(os.path.realpath(__file__)), "clipboard-linux.png")

MAX_HISTORY = 50
MAX_RECENT_EMOJIS = 32
WIN_W, WIN_H, SHADOW = 380, 520, 10

TERMINALS = {
    "gnome-terminal", "gnome-terminal-server", "mate-terminal", "xfce4-terminal",
    "tilix", "terminator", "konsole", "kitty", "alacritty", "wezterm", "guake",
    "tilda", "xterm", "uxterm", "urxvt", "st", "sakura", "lxterminal",
    "qterminal", "terminology", "x-terminal-emulator", "ghostty", "foot",
    "org.gnome.console", "kgx", "warp", "hyper", "tabby",
}

# Emoji groups as numbered in GTK's emoji data.
EMOJI_GROUPS = [
    (0, "😀", "Smileys"), (1, "👋", "People"), (3, "🐵", "Animals & Nature"),
    (4, "🍇", "Food & Drink"), (5, "🌍", "Travel & Places"), (6, "⚽", "Activities"),
    (7, "💡", "Objects"), (8, "❤️", "Symbols"), (9, "🏁", "Flags"),
]

CSS = b"""
window.cb-window { background-color: transparent; }
.cb-frame {
    background-color: @theme_base_color;
    border: 1px solid alpha(@theme_fg_color, 0.16);
    border-radius: 12px;
    box-shadow: 0 3px 10px alpha(black, 0.35);
    margin: 10px;
}
.cb-grip {
    min-width: 40px; min-height: 4px; border-radius: 2px;
    background-color: alpha(@theme_fg_color, 0.30);
    margin-top: 8px;
}
.cb-header { padding: 4px 10px 6px 10px; }
.cb-tabs button { padding: 4px 14px; }
.cb-toolbar { padding: 0 12px 6px 12px; }
list.cb-list, list.cb-list row { background-color: transparent; }
list.cb-list row { padding: 0; outline-offset: -4px; }
list.cb-list row:selected { background-color: transparent; color: @theme_fg_color; }
.cb-card {
    background-color: alpha(@theme_fg_color, 0.06);
    border-radius: 8px;
    padding: 8px 6px 8px 12px;
    margin: 3px 12px;
}
list.cb-list row:hover .cb-card { background-color: alpha(@theme_selected_bg_color, 0.18); }
list.cb-list row:selected .cb-card { background-color: alpha(@theme_selected_bg_color, 0.30); }
.cb-meta { font-size: 0.82em; opacity: 0.6; }
.cb-section { font-size: 0.78em; font-weight: bold; opacity: 0.6; margin: 8px 16px 2px 16px; }
.cb-small-btn { padding: 2px; min-width: 24px; min-height: 24px; }
.cb-pinned { color: @theme_selected_bg_color; }
.cb-empty { opacity: 0.6; margin: 40px 20px; }
.cb-emoji { font-size: 22px; padding: 2px; min-width: 38px; min-height: 38px; }
.cb-cat { font-size: 17px; padding: 2px 6px; }
.cb-cat:checked { background-color: alpha(@theme_selected_bg_color, 0.30); }
.cb-thumb { border-radius: 4px; }
"""


def human_age(ts):
    s = max(0, int(time.time() - ts))
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


def pixbuf_hash(pb):
    data = pb.read_pixel_bytes().get_data()
    return hashlib.sha1(f"{pb.get_width()}x{pb.get_height()}".encode() + data).hexdigest()


def scale_to_fit(pb, max_w, max_h):
    w, h = pb.get_width(), pb.get_height()
    k = min(max_w / w, max_h / h, 1.0)
    if k >= 1.0:
        return pb
    return pb.scale_simple(max(1, int(w * k)), max(1, int(h * k)), GdkPixbuf.InterpType.BILINEAR)


class Item:
    def __init__(self, kind, text=None, pixbuf=None, digest=None, pinned=False, ts=None):
        self.kind = kind
        self.text = text
        self.pixbuf = pixbuf
        self.pinned = pinned
        self.time = ts or time.time()
        if digest:
            self.hash = digest
        elif kind == "text":
            self.hash = hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()
        else:
            self.hash = pixbuf_hash(pixbuf)

    def matches(self, query):
        if not query:
            return True
        if self.kind == "image":
            return query in "image picture screenshot photo"
        return query in self.text.lower()


class KeySender:
    """Sends a key combination to the focused window through the XTest extension."""

    def __init__(self):
        self.ok = False
        try:
            self.x11 = ctypes.cdll.LoadLibrary("libX11.so.6")
            self.xtst = ctypes.cdll.LoadLibrary("libXtst.so.6")
        except OSError:
            return
        self.x11.XOpenDisplay.restype = ctypes.c_void_p
        self.x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x11.XKeysymToKeycode.restype = ctypes.c_ubyte
        self.x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.x11.XFlush.argtypes = [ctypes.c_void_p]
        self.xtst.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        self.dpy = self.x11.XOpenDisplay(None)
        self.ok = bool(self.dpy)

    def send(self, keysyms):
        if not self.ok:
            return False
        codes = [self.x11.XKeysymToKeycode(self.dpy, k) for k in keysyms]
        for c in codes:
            self.xtst.XTestFakeKeyEvent(self.dpy, c, 1, 0)
        for c in reversed(codes):
            self.xtst.XTestFakeKeyEvent(self.dpy, c, 0, 0)
        self.x11.XFlush(self.dpy)
        return True


def window_class(xid):
    if not xid or not shutil.which("xprop"):
        return ""
    try:
        out = subprocess.run(["xprop", "-id", str(xid), "WM_CLASS"],
                             capture_output=True, text=True, timeout=1).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.split("=", 1)[-1].replace('"', "").lower()


def load_emojis():
    data = Gio.resources_lookup_data("/org/gtk/libgtk/emoji/en.data", 0)
    variant = GLib.Variant.new_from_bytes(GLib.VariantType("a(ausasu)"), data, True)
    emojis = []
    for codes, name, keywords, group in variant.unpack():
        if group == 2:  # bare skin-tone modifiers
            continue
        char = "".join(chr(c) for c in codes if c)
        emojis.append((char, name, " ".join([name.lower()] + [k.lower() for k in keywords]), group))
    return emojis


class ClipBoardApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.items = []
        self.recent_emojis = []
        self.win = None
        self.prev_xid = None
        self.ignore_until = 0.0
        self.dragging = False
        self.user_pos = None
        self.emojis = None
        self.emoji_group = None
        self.keys = KeySender()

    # ---------- lifecycle ----------

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.hold()
        os.makedirs(IMAGE_DIR, exist_ok=True)
        os.makedirs(os.path.join(CACHE_DIR, "drag"), exist_ok=True)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        for name, cb in (("toggle", lambda *a: self.toggle()),
                         ("show", lambda *a: self.show_popup()),
                         ("quit", lambda *a: self.quit())):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)

        self.load_pins()
        self.clip = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        self.clip.connect("owner-change", self.on_owner_change)
        self.build_window()
        self.build_tray()
        # Capture whatever is on the clipboard right now.
        self.clip.request_targets(self.on_targets)

    def do_command_line(self, cmdline):
        args = cmdline.get_arguments()[1:]
        if "--quit" in args:
            self.quit()
        elif "--toggle" in args:
            self.toggle()
        elif "--show" in args:
            self.show_popup()
        return 0

    def do_activate(self):
        self.show_popup()

    # ---------- persistence ----------

    def load_pins(self):
        try:
            with open(PINS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        self.recent_emojis = data.get("recent_emojis", [])[:MAX_RECENT_EMOJIS]
        for p in data.get("pins", []):
            try:
                if p["kind"] == "text":
                    self.items.append(Item("text", text=p["text"], pinned=True, ts=p.get("time")))
                else:
                    pb = GdkPixbuf.Pixbuf.new_from_file(os.path.join(IMAGE_DIR, p["file"]))
                    self.items.append(Item("image", pixbuf=pb, digest=p["hash"], pinned=True, ts=p.get("time")))
            except (KeyError, GLib.Error):
                continue

    def save_pins(self):
        pins, keep = [], set()
        for it in self.items:
            if not it.pinned:
                continue
            if it.kind == "text":
                pins.append({"kind": "text", "text": it.text, "time": it.time})
            else:
                fname = it.hash + ".png"
                path = os.path.join(IMAGE_DIR, fname)
                if not os.path.exists(path):
                    it.pixbuf.savev(path, "png", [], [])
                keep.add(fname)
                pins.append({"kind": "image", "file": fname, "hash": it.hash, "time": it.time})
        for fname in os.listdir(IMAGE_DIR):
            if fname not in keep:
                try:
                    os.remove(os.path.join(IMAGE_DIR, fname))
                except OSError:
                    pass
        tmp = PINS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "pins": pins, "recent_emojis": self.recent_emojis}, f, ensure_ascii=False)
        os.replace(tmp, PINS_FILE)

    # ---------- clipboard monitoring ----------

    def on_owner_change(self, clip, event):
        if time.monotonic() < self.ignore_until:
            return
        clip.request_targets(self.on_targets)

    def on_targets(self, clip, atoms, *rest):
        if not atoms:
            return
        names = {a.name() for a in atoms}
        if "x-kde-passwordManagerHint" in names:  # don't record passwords from password managers
            return
        if Gtk.targets_include_text(atoms):
            clip.request_text(self.on_text)
        elif Gtk.targets_include_image(atoms, False):
            clip.request_image(self.on_image)

    def on_text(self, clip, text, *rest):
        if text and text.strip():
            self.add_item(Item("text", text=text))

    def on_image(self, clip, pixbuf, *rest):
        if pixbuf is not None:
            self.add_item(Item("image", pixbuf=pixbuf))

    def add_item(self, new):
        for it in self.items:
            if it.hash == new.hash:
                it.time = time.time()
                if not it.pinned:
                    self.items.remove(it)
                    self.items.insert(self.first_unpinned_index(), it)
                break
        else:
            self.items.insert(self.first_unpinned_index(), new)
            unpinned = [i for i in self.items if not i.pinned]
            for old in unpinned[MAX_HISTORY:]:
                self.items.remove(old)
        if self.win.get_visible():
            self.refresh_list()

    def first_unpinned_index(self):
        for n, it in enumerate(self.items):
            if not it.pinned:
                return n
        return len(self.items)

    # ---------- pasting ----------

    def set_clipboard(self, item):
        self.ignore_until = time.monotonic() + 0.6
        if item.kind == "text":
            self.clip.set_text(item.text, -1)
        else:
            self.clip.set_image(item.pixbuf)

    def paste_item(self, item):
        self.set_clipboard(item)
        if not item.pinned:
            item.time = time.time()
            self.items.remove(item)
            self.items.insert(self.first_unpinned_index(), item)
        self.hide_popup()
        GLib.timeout_add(120, self.focus_previous_and_paste, None)

    def paste_emoji(self, char):
        if char in self.recent_emojis:
            self.recent_emojis.remove(char)
        self.recent_emojis.insert(0, char)
        del self.recent_emojis[MAX_RECENT_EMOJIS:]
        self.save_pins()
        restore = next((i for i in self.items if not i.pinned), None)
        self.set_clipboard(Item("text", text=char))
        self.hide_popup()
        GLib.timeout_add(120, self.focus_previous_and_paste, restore)

    def focus_previous_and_paste(self, restore):
        if self.prev_xid:
            try:
                display = GdkX11.X11Display.get_default()
                prev = GdkX11.X11Window.foreign_new_for_display(display, self.prev_xid)
                if prev:
                    prev.focus(GdkX11.x11_get_server_time(self.win.get_window()))
            except (TypeError, GLib.Error):
                pass
        GLib.timeout_add(80, self.send_paste_keys, restore)
        return False

    def send_paste_keys(self, restore):
        wm_class = window_class(self.prev_xid)
        is_terminal = any(t in [p.strip() for p in wm_class.split(",")] for t in TERMINALS)
        keys = [Gdk.KEY_Control_L, Gdk.KEY_Shift_L, Gdk.KEY_v] if is_terminal else [Gdk.KEY_Control_L, Gdk.KEY_v]
        self.keys.send(keys)
        if restore is not None:
            # An emoji should not replace what the user had copied: put it back once pasted.
            GLib.timeout_add(500, lambda: (self.set_clipboard(restore), False)[1])
        return False

    # ---------- window ----------

    def build_window(self):
        win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL, title=APP_NAME)
        win.get_style_context().add_class("cb-window")
        self.add_window(win)
        win.set_decorated(False)
        win.set_resizable(False)
        win.set_keep_above(True)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        win.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        win.set_default_size(WIN_W + 2 * SHADOW, WIN_H + 2 * SHADOW)
        if os.path.exists(ICON_FILE):
            win.set_icon_from_file(ICON_FILE)
        visual = win.get_screen().get_rgba_visual()
        if visual and win.get_screen().is_composited():
            win.set_visual(visual)
            win.set_app_paintable(True)
        win.connect("delete-event", lambda *a: self.hide_popup() or True)
        win.connect("key-press-event", self.on_key)
        win.connect("focus-out-event", self.on_focus_out)
        win.connect("configure-event", self.on_configure)
        self.win = win

        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        frame.get_style_context().add_class("cb-frame")
        win.add(frame)

        # Header: drag handle + tabs + close.
        header_events = Gtk.EventBox()
        header_events.connect("button-press-event", self.on_header_press)
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header_events.add(header)
        grip = Gtk.Box(halign=Gtk.Align.CENTER)
        grip.get_style_context().add_class("cb-grip")
        header.pack_start(grip, False, False, 0)
        bar = Gtk.Box(spacing=6)
        bar.get_style_context().add_class("cb-header")
        header.pack_start(bar, False, False, 0)
        frame.pack_start(header_events, False, False, 0)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120)
        switcher = Gtk.StackSwitcher(stack=self.stack)
        switcher.get_style_context().add_class("cb-tabs")
        bar.pack_start(switcher, False, False, 0)
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_tooltip_text("Close (Esc)")
        close.connect("clicked", lambda *a: self.hide_popup())
        bar.pack_end(close, False, False, 0)
        frame.pack_start(self.stack, True, True, 0)

        self.stack.add_titled(self.build_clipboard_page(), "clipboard", "📋  Clipboard")
        self.stack.add_titled(self.build_emoji_page(), "emojis", "😀  Emojis")
        self.stack.connect("notify::visible-child-name", self.on_page_changed)
        frame.show_all()

    def build_clipboard_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        tools = Gtk.Box(spacing=6)
        tools.get_style_context().add_class("cb-toolbar")
        self.search = Gtk.SearchEntry(placeholder_text="Search history...")
        self.search.connect("search-changed", lambda *a: (self.list.invalidate_filter(), self.list.invalidate_headers()))
        self.search.connect("key-press-event", self.on_search_key)
        self.search.connect("activate", lambda *a: self.activate_first_row())
        tools.pack_start(self.search, True, True, 0)
        clear = Gtk.Button(label="Clear all")
        clear.set_tooltip_text("Clear unpinned history")
        clear.connect("clicked", self.on_clear)
        tools.pack_start(clear, False, False, 0)
        page.pack_start(tools, False, False, 0)

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, activate_on_single_click=True)
        self.list.get_style_context().add_class("cb-list")
        self.list.set_filter_func(lambda row: row.item.matches(self.search.get_text().strip().lower()))
        self.list.set_header_func(self.list_header)
        self.list.connect("row-activated", lambda lb, row: self.paste_item(row.item))
        empty = Gtk.Label(label="Clipboard is empty\n\nCopy text or an image with Ctrl+C\nand it will show up here.",
                          justify=Gtk.Justification.CENTER)
        empty.get_style_context().add_class("cb-empty")
        empty.show()
        self.list.set_placeholder(empty)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.add(self.list)
        page.pack_start(scroller, True, True, 0)
        return page

    def list_header(self, row, before):
        label = None
        query = self.search.get_text().strip()
        if row.item.pinned and (before is None or not before.get_child_visible()) and not query:
            label = "PINNED"
        elif not row.item.pinned and not query and any(i.pinned for i in self.items):
            if before is None or before.item.pinned:
                label = "RECENT"
        if label:
            if not isinstance(row.get_header(), Gtk.Label):
                h = Gtk.Label(xalign=0)
                h.get_style_context().add_class("cb-section")
                row.set_header(h)
            row.get_header().set_text(label)
        else:
            row.set_header(None)

    def refresh_list(self):
        selected = self.list.get_selected_row()
        selected_hash = selected.item.hash if selected else None
        for child in self.list.get_children():
            child.destroy()
        for it in self.items:
            row = self.make_row(it)
            self.list.add(row)
            if it.hash == selected_hash:
                self.list.select_row(row)
        self.list.show_all()

    def make_row(self, item):
        row = Gtk.ListBoxRow()
        row.item = item
        card = Gtk.Box(spacing=6)
        card.get_style_context().add_class("cb-card")
        row.add(card)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        if item.kind == "text":
            preview = item.text.strip()[:600].replace("\t", "    ")
            label = Gtk.Label(label=preview, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                              ellipsize=Pango.EllipsizeMode.END, lines=4, max_width_chars=40)
            body.pack_start(label, False, False, 0)
            meta = human_age(item.time)
            if len(item.text) > 600 or item.text.count("\n") > 3:
                meta += f"  ·  {len(item.text)} chars"
        else:
            thumb = Gtk.Image.new_from_pixbuf(scale_to_fit(item.pixbuf, WIN_W - 110, 120))
            thumb.set_halign(Gtk.Align.START)
            thumb.get_style_context().add_class("cb-thumb")
            body.pack_start(thumb, False, False, 0)
            meta = f"Image  ·  {item.pixbuf.get_width()}×{item.pixbuf.get_height()}  ·  {human_age(item.time)}"
        meta_label = Gtk.Label(label=meta, xalign=0)
        meta_label.get_style_context().add_class("cb-meta")
        body.pack_start(meta_label, False, False, 0)
        card.pack_start(body, True, True, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        pin_icon = "view-pin-symbolic" if Gtk.IconTheme.get_default().has_icon("view-pin-symbolic") else "starred-symbolic"
        if not item.pinned and pin_icon == "starred-symbolic":
            pin_icon = "non-starred-symbolic"
        pin = Gtk.Button.new_from_icon_name(pin_icon, Gtk.IconSize.MENU)
        pin.set_tooltip_text("Unpin" if item.pinned else "Pin (keep forever)")
        delete = Gtk.Button.new_from_icon_name("edit-delete-symbolic", Gtk.IconSize.MENU)
        delete.set_tooltip_text("Delete")
        for b in (pin, delete):
            b.set_relief(Gtk.ReliefStyle.NONE)
            b.set_can_focus(False)
            b.get_style_context().add_class("cb-small-btn")
            buttons.pack_start(b, False, False, 0)
        if item.pinned:
            pin.get_style_context().add_class("cb-pinned")
        else:
            pin.set_opacity(0.55)
        pin.connect("clicked", lambda *a: self.toggle_pin(item))
        delete.connect("clicked", lambda *a: self.delete_item(item))
        card.pack_end(buttons, False, False, 0)

        self.setup_drag(row, item)
        return row

    def setup_drag(self, row, item):
        targets = Gtk.TargetList.new([])
        if item.kind == "text":
            targets.add_text_targets(1)
        else:
            targets.add_uri_targets(2)
            targets.add_image_targets(3, True)
        row.drag_source_set(Gdk.ModifierType.BUTTON1_MASK, [], Gdk.DragAction.COPY)
        row.drag_source_set_target_list(targets)
        row.connect("drag-begin", self.on_drag_begin, item)
        row.connect("drag-data-get", self.on_drag_data_get, item)
        row.connect("drag-end", self.on_drag_end)

    def on_drag_begin(self, row, ctx, item):
        self.dragging = True
        if item.kind == "image":
            Gtk.drag_set_icon_pixbuf(ctx, scale_to_fit(item.pixbuf, 96, 96), 0, 0)
        else:
            Gtk.drag_set_icon_name(ctx, "text-x-generic", 0, 0)

    def on_drag_data_get(self, row, ctx, selection, info, ts, item):
        if info == 1:
            selection.set_text(item.text, -1)
        elif info == 2:
            path = os.path.join(CACHE_DIR, "drag", f"clipboard-{item.hash[:10]}.png")
            if not os.path.exists(path):
                item.pixbuf.savev(path, "png", [], [])
            selection.set_uris([GLib.filename_to_uri(path, None)])
        elif info == 3:
            selection.set_pixbuf(item.pixbuf)

    def on_drag_end(self, row, ctx):
        self.dragging = False
        GLib.timeout_add(200, self.hide_if_unfocused)

    def toggle_pin(self, item):
        item.pinned = not item.pinned
        # Pinned items sit above the recent ones; both cases land at the boundary.
        self.items.remove(item)
        self.items.insert(self.first_unpinned_index(), item)
        self.save_pins()
        self.refresh_list()

    def delete_item(self, item):
        self.items.remove(item)
        if item.pinned:
            self.save_pins()
        self.refresh_list()

    def on_clear(self, *a):
        self.items = [i for i in self.items if i.pinned]
        self.refresh_list()

    def activate_first_row(self):
        row = self.first_visible_row()
        if row:
            self.paste_item(row.item)

    def first_visible_row(self):
        for row in self.list.get_children():
            if row.get_child_visible():
                return row
        return None

    # ---------- emoji page ----------

    def build_emoji_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        tools = Gtk.Box()
        tools.get_style_context().add_class("cb-toolbar")
        self.emoji_search = Gtk.SearchEntry(placeholder_text="Search emojis (love, lol, fire, pizza...)")
        self.emoji_search.connect("search-changed", lambda *a: self.fill_emojis())
        tools.pack_start(self.emoji_search, True, True, 0)
        page.pack_start(tools, False, False, 0)

        cat_scroll = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER,
                                        hscrollbar_policy=Gtk.PolicyType.EXTERNAL)
        cat_scroll.add_events(Gdk.EventMask.SCROLL_MASK | Gdk.EventMask.SMOOTH_SCROLL_MASK)
        cat_scroll.connect("scroll-event", self.on_category_scroll)
        cats = Gtk.Box(spacing=2, margin_start=10, margin_end=10, margin_bottom=4)
        cat_scroll.add(cats)
        self.cat_buttons = {}
        first = None
        for group, icon, title in [(-1, "🕘", "Recently used")] + EMOJI_GROUPS:
            b = Gtk.RadioButton.new_from_widget(first)
            b.set_mode(False)
            b.set_label(icon)
            b.set_relief(Gtk.ReliefStyle.NONE)
            b.set_tooltip_text(title)
            b.get_style_context().add_class("cb-cat")
            b.connect("toggled", self.on_category_toggled, group)
            cats.pack_start(b, False, False, 0)
            self.cat_buttons[group] = b
            first = first or b
        page.pack_start(cat_scroll, False, False, 0)

        self.emoji_flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                      min_children_per_line=6, max_children_per_line=8,
                                      valign=Gtk.Align.START, margin_start=8, margin_end=8)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.add(self.emoji_flow)
        self.emoji_scroller = scroller
        page.pack_start(scroller, True, True, 0)
        self.emoji_status = Gtk.Label(xalign=0.5)
        self.emoji_status.get_style_context().add_class("cb-empty")
        page.pack_start(self.emoji_status, False, False, 0)
        return page

    def on_page_changed(self, *a):
        if self.stack.get_visible_child_name() == "emojis":
            if self.emojis is None:
                self.emojis = load_emojis()
                self.emoji_group = -1 if self.recent_emojis else 0
                self.cat_buttons[self.emoji_group].set_active(True)
                self.fill_emojis()
            self.emoji_search.grab_focus()
        else:
            self.focus_list()

    def on_category_toggled(self, button, group):
        if button.get_active() and self.emojis is not None:
            self.emoji_group = group
            self.emoji_search.set_text("")
            self.fill_emojis()

    def on_category_scroll(self, scroller, event):
        adj = scroller.get_hadjustment()
        ok, dx, dy = event.get_scroll_deltas()
        if not ok:
            dy = {Gdk.ScrollDirection.UP: -1, Gdk.ScrollDirection.LEFT: -1}.get(event.direction, 1)
            dx = 0
        adj.set_value(adj.get_value() + (dx + dy) * 40)
        return True

    def fill_emojis(self):
        if self.emojis is None:
            return
        for child in self.emoji_flow.get_children():
            child.destroy()
        query = self.emoji_search.get_text().strip().lower()
        if query:
            words = query.split()
            chosen = [e for e in self.emojis if all(w in e[2] for w in words)]
            # Exact name first, then whole-keyword matches, then names starting with the query.
            q = words[0]
            chosen.sort(key=lambda e: (e[1].lower() != q, f" {q} " not in f" {e[2]} ", not e[1].lower().startswith(q)))
            chosen = chosen[:200]
        elif self.emoji_group == -1:
            names = {e[0]: e for e in self.emojis}
            chosen = [names.get(c, (c, c, c, -1)) for c in self.recent_emojis]
        else:
            chosen = [e for e in self.emojis if e[3] == self.emoji_group]
        for char, name, _kw, _g in chosen:
            b = Gtk.Button(label=char, relief=Gtk.ReliefStyle.NONE)
            b.get_style_context().add_class("cb-emoji")
            b.set_tooltip_text(name)
            b.connect("clicked", lambda btn, c=char: self.paste_emoji(c))
            self.emoji_flow.add(b)
        self.emoji_flow.show_all()
        self.emoji_scroller.get_vadjustment().set_value(0)
        if not chosen:
            self.emoji_status.set_text("No emojis found" if query else "Emojis you use will show up here")
            self.emoji_status.show()
        else:
            self.emoji_status.hide()

    # ---------- show / hide ----------

    def toggle(self):
        if self.win.get_visible():
            self.hide_popup()
        else:
            self.show_popup()

    def show_popup(self):
        active = Gdk.Screen.get_default().get_active_window()
        own = self.win.get_window()
        if active is not None and (own is None or active.get_xid() != own.get_xid()):
            self.prev_xid = active.get_xid()
        self.search.set_text("")
        self.refresh_list()
        self.place_window()
        self.win.show()
        self.win.present_with_time(GdkX11.x11_get_server_time(self.win.get_window()))
        if self.stack.get_visible_child_name() == "emojis":
            self.emoji_search.set_text("")
            self.emoji_search.grab_focus()
        else:
            self.focus_list()

    def focus_list(self):
        row = self.first_visible_row()
        if row:
            self.list.select_row(row)
            row.grab_focus()
        else:
            self.search.grab_focus()

    def place_window(self):
        w, h = WIN_W + 2 * SHADOW, WIN_H + 2 * SHADOW
        display = Gdk.Display.get_default()
        if self.user_pos:
            x, y = self.user_pos
            monitor = display.get_monitor_at_point(x + w // 2, y + h // 2)
        else:
            _screen, px, py = display.get_default_seat().get_pointer().get_position()
            monitor = display.get_monitor_at_point(px, py)
            x, y = px - w // 2, py + 12
        area = monitor.get_workarea()
        x = max(area.x, min(x, area.x + area.width - w))
        y = max(area.y, min(y, area.y + area.height - h))
        self.win.move(x, y)

    def hide_popup(self):
        if self.win.get_visible():
            self.win.hide()

    def hide_if_unfocused(self):
        if self.win.get_visible() and not self.win.is_active() and not self.dragging:
            self.hide_popup()
        return False

    def on_focus_out(self, *a):
        GLib.timeout_add(150, self.hide_if_unfocused)
        return False

    def on_header_press(self, widget, event):
        if event.button == 1 and event.type == Gdk.EventType.BUTTON_PRESS:
            self.moving = True
            self.win.begin_move_drag(event.button, int(event.x_root), int(event.y_root), event.time)
            return True
        return False

    def on_configure(self, win, event):
        if getattr(self, "moving", False):
            self.user_pos = win.get_position()
        return False

    def on_key(self, win, event):
        key = event.keyval
        page = self.stack.get_visible_child_name()
        entry = self.search if page == "clipboard" else self.emoji_search
        if key == Gdk.KEY_Escape:
            if entry.get_text():
                entry.set_text("")
            else:
                self.hide_popup()
            return True
        if page == "clipboard" and not self.search.has_focus():
            row = self.list.get_selected_row()
            if key == Gdk.KEY_Delete and row:
                self.delete_item(row.item)
                self.focus_list()
                return True
            if key in (Gdk.KEY_Up, Gdk.KEY_Down, Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_Tab,
                       Gdk.KEY_ISO_Left_Tab, Gdk.KEY_Page_Up, Gdk.KEY_Page_Down, Gdk.KEY_Home, Gdk.KEY_End):
                return False
            return self.search.handle_event(event)
        if page == "emojis" and not self.emoji_search.has_focus():
            return self.emoji_search.handle_event(event)
        return False

    def on_search_key(self, entry, event):
        if event.keyval == Gdk.KEY_Down:
            self.focus_list()
            return True
        return False

    # ---------- tray icon ----------

    def build_tray(self):
        if XApp is None:
            return
        try:
            icon = XApp.StatusIcon()
        except Exception:
            return
        icon.set_name("clipboard-linux")
        icon.set_icon_name(ICON_FILE if os.path.exists(ICON_FILE) else "edit-paste")
        icon.set_tooltip_text("ClipBoard — Super+V to open")
        menu = Gtk.Menu()
        for label, cb in (("Open clipboard  (Super+V)", lambda *a: self.show_popup()),
                          ("Clear history", self.on_clear),
                          ("Quit ClipBoard", lambda *a: self.quit())):
            mi = Gtk.MenuItem(label=label)
            mi.connect("activate", cb)
            menu.append(mi)
        menu.show_all()
        icon.set_secondary_menu(menu)
        icon.connect("activate", lambda *a: self.toggle())
        self.tray = icon


def main():
    if os.environ.get("XDG_SESSION_TYPE") == "wayland":
        print("ClipBoard needs an X11 session (Linux Mint Cinnamon uses X11 by default).", file=sys.stderr)
    app = ClipBoardApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
