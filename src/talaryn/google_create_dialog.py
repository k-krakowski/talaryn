"""One GTK window for naming, creating, and refreshing a Google document."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

from .google_create import KINDS, clean_title, create_google_file, refresh_created_file
from .i18n import tr
from .paths import APP_ICON_FILE, APP_ICON_NAME, GOOGLE_CREATE_APP_ID, data_dirs
from .window_position import place_at_pointer, set_native_parent


class GoogleCreateWindow(Adw.ApplicationWindow):
    def __init__(self, app, profile_id: str, profile: dict, folder: str, kind: str, context: dict):
        super().__init__(application=app, title=tr(KINDS[kind]["label"]))
        self.profile_id, self.profile, self.folder, self.kind = profile_id, profile, folder, kind
        self.running = False
        self.result = None
        self.title_text = ""
        self._pulse_source = None
        self._positioned = False
        self._reported_path = None
        self.set_icon_name(APP_ICON_NAME)
        self.set_default_size(460, 280)
        self.set_resizable(False)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.HeaderBar()
        title_box = Gtk.Box(spacing=8)
        logo = Gtk.Image(icon_name=APP_ICON_NAME, pixel_size=24)
        for root in data_dirs():
            icon = root / "icons" / APP_ICON_FILE
            if icon.is_file():
                logo.set_from_file(str(icon))
                break
        title_box.append(logo)
        title_box.append(Gtk.Label(label=tr(KINDS[kind]["label"])))
        header.set_title_widget(title_box)
        outer.append(header)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        for edge in ("top", "bottom", "start", "end"):
            getattr(content, f"set_margin_{edge}")(20)
        content.append(Gtk.Label(label=tr("file_name_prompt"), xalign=0))
        self.entry = Gtk.Entry(text=tr(KINDS[kind]["default_name"]))
        self.entry.connect("activate", self._start)
        content.append(self.entry)
        self.status = Gtk.Label(xalign=0, wrap=True, max_width_chars=50)
        self.status.set_selectable(True)
        content.append(self.status)
        self.progress = Gtk.ProgressBar(visible=False)
        content.append(self.progress)
        buttons = Gtk.Box(spacing=10, halign=Gtk.Align.END)
        self.cancel = Gtk.Button(label=tr("cancel"))
        self.cancel.connect("clicked", lambda _button: self.close())
        buttons.append(self.cancel)
        self.create = Gtk.Button(label=tr("google_create_button"))
        self.create.add_css_class("suggested-action")
        self.create.connect("clicked", self._start)
        buttons.append(self.create)
        content.append(buttons)
        outer.append(content)
        self.set_content(outer)
        self.connect("close-request", lambda _window: self.running)
        self.connect("map", self._on_map, context.get("pointer"))
        set_native_parent(self, context.get("parent", ""))
        self.entry.select_region(0, -1)
        self.entry.grab_focus()

    def _on_map(self, _window, pointer):
        if not self._positioned:
            self._positioned = True
            GLib.idle_add(self._place, pointer)

    def _place(self, pointer):
        place_at_pointer(self, pointer)
        return GLib.SOURCE_REMOVE

    def _set_status(self, text):
        self.status.set_text(text)
        return GLib.SOURCE_REMOVE

    def _pulse(self):
        self.progress.pulse()
        return GLib.SOURCE_CONTINUE

    def _busy(self):
        self.running = True
        self.entry.set_sensitive(False)
        self.create.set_sensitive(False)
        self.cancel.set_sensitive(False)
        self.progress.set_visible(True)
        self._pulse_source = GLib.timeout_add(100, self._pulse)

    def _start(self, _widget):
        if self.running:
            return
        if self.result is not None and self.result.created:
            # Only retry directory refresh after a successful upload.
            self._busy()
            threading.Thread(target=self._retry_refresh, daemon=True).start()
            return
        try:
            self.title_text = clean_title(self.entry.get_text())
        except ValueError as error:
            self.status.set_text(str(error))
            return
        self.entry.set_text(self.title_text)
        self._busy()
        self.status.set_text(tr("google_name_checking"))
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self):
        try:
            result = create_google_file(self.profile_id, self.profile, self.folder, self.kind,
                self.title_text, lambda text: GLib.idle_add(self._set_status, text),
                lambda path: GLib.idle_add(self._report_visible, path))
        except Exception as error:
            # Keep unexpected errors visible instead of leaving an endless progress bar.
            from .google_create import CreationResult
            result = CreationResult(False, str(error))
        GLib.idle_add(self._finished, result)

    def _ready(self):
        self.running = False
        if self._pulse_source is not None:
            GLib.source_remove(self._pulse_source)
            self._pulse_source = None
        self.progress.set_visible(False)
        self.cancel.set_sensitive(True)

    def _finished(self, result):
        self._ready()
        self.result = result
        if not result.created:
            self.status.set_text(tr("google_file_create_failed") + "\n" + result.detail)
            self.entry.set_sensitive(True)
            self.create.set_sensitive(True)
            self.entry.grab_focus()
            return GLib.SOURCE_REMOVE
        self.entry.set_sensitive(False)
        if result.local_path:
            self._report_visible(result.local_path)
        detail = result.detail
        if result.url:
            try:
                subprocess.Popen(["xdg-open", result.url], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except OSError:
                detail = "\n".join(filter(None, (detail, tr("xdg_open_missing"))))
        if detail:
            self.status.set_text(tr("google_file_created") + "\n" + detail)
            self.cancel.set_label(tr("close"))
            self.create.set_label(tr("google_refresh_retry"))
            self.create.set_visible(result.refresh_warning)
            self.create.set_sensitive(result.refresh_warning)
        else:
            self.close()
        return GLib.SOURCE_REMOVE

    def _report_visible(self, path):
        # Nautilus reads this immediately, even if a warning keeps this window open.
        if path != self._reported_path:
            self._reported_path = path
            print(json.dumps({"local_path": str(path)}, ensure_ascii=False), flush=True)
        return GLib.SOURCE_REMOVE

    def _retry_refresh(self):
        GLib.idle_add(self._set_status, tr("google_folder_refreshing"))
        try:
            path = refresh_created_file(self.profile_id, self.profile, Path(self.folder),
                                       self.title_text, KINDS[self.kind]["ext"])
        except (OSError, RuntimeError, ValueError):
            path = None
        GLib.idle_add(self._refresh_finished, path)

    def _refresh_finished(self, path):
        self._ready()
        if path:
            self._report_visible(path)
            self.close()
        else:
            self.status.set_text(tr("google_file_created") + "\n" + tr("google_refresh_failed"))
            self.create.set_sensitive(True)
        return GLib.SOURCE_REMOVE


class GoogleCreateApplication(Adw.Application):
    def __init__(self, profile_id, profile, folder, kind, context):
        super().__init__(application_id=GOOGLE_CREATE_APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.arguments = profile_id, profile, folder, kind, context
        self.window = None

    def do_activate(self):
        if self.window is None:
            self.window = GoogleCreateWindow(self, *self.arguments)
        self.window.present()


def run_google_create(profile_id, profile, folder, kind, context) -> int:
    return GoogleCreateApplication(profile_id, profile, folder, kind, context).run(["talaryn-google-create"])
