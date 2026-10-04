#!/usr/bin/env python3
# Copyright (c) 2026 Kamil Krakowski
# SPDX-License-Identifier: MIT
"""Nautilus menu launching the asynchronous Talaryn Google creation dialog."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path
import sys

_extension_dir = str(Path(__file__).resolve().parent)
if _extension_dir not in sys.path:
    sys.path.insert(0, _extension_dir)
from talaryn_i18n import tr, _roots

import gi

gi.require_version("Nautilus", "4.0")
from gi.repository import Gio, GLib, GObject, Nautilus

KINDS = {
    "doc": {
        "label": "google_new_doc",
        "default_name": "google_default_doc",
        "template": "blank.docx",
        "ext": "docx",
        "url_template": "https://docs.google.com/document/d/{file_id}/edit",
    },
    "sheet": {
        "label": "google_new_sheet",
        "default_name": "google_default_sheet",
        "template": "blank.xlsx",
        "ext": "xlsx",
        "url_template": "https://docs.google.com/spreadsheets/d/{file_id}/edit",
    },
    "slides": {
        "label": "google_new_slides",
        "default_name": "google_default_slides",
        "template": "blank.pptx",
        "ext": "pptx",
        "url_template": "https://docs.google.com/presentation/d/{file_id}/edit",
    },
}


def _config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "talaryn"


def _absolute_path(value: str) -> Path:
    return Path(os.path.abspath(os.path.expandvars(os.path.expanduser(value))))


def _load_profiles() -> dict[str, dict]:
    path = _config_dir() / "profiles.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    profiles = data.get("profiles", {}) if isinstance(data, dict) else {}
    return {str(k): v for k, v in profiles.items() if isinstance(v, dict)}


def _load_settings() -> dict:
    try:
        data = json.loads((_config_dir() / "settings.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _setting_enabled(key: str, default: bool = True) -> bool:
    return bool(_load_settings().get(key, default))


def _notify(title: str, body: str) -> None:
    try:
        subprocess.run(
            ["notify-send", title, body],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass



class GoogleNewDocsProvider(GObject.GObject, Nautilus.MenuProvider):
    def get_background_items(self, current_folder):
        if not _setting_enabled("context_google_new_docs"):
            return []

        folder_path = self._folder_to_path(current_folder)
        match = self._profile_for_path(folder_path) if folder_path else None
        if not match:
            return []

        profile_id, _profile, _mount_dir = match
        submenu = Nautilus.Menu()

        for key, cfg in KINDS.items():
            item = Nautilus.MenuItem(
                name=f"GoogleNewDocsProvider::{profile_id}::{key}",
                label=tr(cfg["label"]),
                tip=tr("google_new_help"),
            )
            item.connect("activate", self._create_google_file, profile_id, folder_path, key)
            submenu.append_item(item)

        top = Nautilus.MenuItem(
            name=f"GoogleNewDocsProvider::{profile_id}::top",
            label=tr("context_google_new_docs"),
            tip=tr("google_new_help"),
        )
        top.set_submenu(submenu)
        return [top]

    def _folder_to_path(self, folder):
        location = folder.get_location()
        if location is None:
            return None

        path = location.get_path()
        if path is None:
            return None

        return _absolute_path(path)

    def _profile_for_path(self, folder_path: Path):
        for profile_id, profile in _load_profiles().items():
            if str(profile.get("kind", "")) != "gdrive":
                continue

            mount_dir = str(profile.get("mount_dir", "")).strip()
            if not mount_dir:
                continue

            mount_path = _absolute_path(mount_dir)
            try:
                folder_path.relative_to(mount_path)
            except ValueError:
                continue
            return profile_id, profile, mount_path

        return None

    def _create_google_file(self, _item, profile_id: str, folder: Path, kind: str):
        # Capture the parent and pointer at activation, before launching another app.
        try:
            gi.require_version("Gtk", "4.0")
            from gi.repository import Gtk
            application = Gio.Application.get_default()
            parent = application.get_active_window() if isinstance(application, Gtk.Application) else None
        except (ImportError, ValueError, AttributeError):
            parent = None
        context = {}
        surface = parent.get_surface() if parent else None
        try:
            gi.require_version("GdkX11", "4.0")
            from gi.repository import Gdk, GdkX11
            if isinstance(Gdk.Display.get_default(), GdkX11.X11Display):
                context["pointer"] = _capture_pointer()
            if isinstance(surface, GdkX11.X11Surface):
                context["parent"] = f"x11:{surface.get_xid():x}"
        except (ImportError, ValueError, AttributeError):
            pass

        # Keep an exported Wayland handle alive for the lifetime of the child.
        try:
            gi.require_version("GdkWayland", "4.0")
            from gi.repository import GdkWayland
            if isinstance(surface, GdkWayland.WaylandToplevel):
                def exported(_surface, handle, *_args):
                    context["parent"] = "wayland:" + handle
                    self._launch(profile_id, folder, kind, context, parent, surface)
                if surface.export_handle(exported):
                    return
        except (ImportError, ValueError, AttributeError):
            pass
        self._launch(profile_id, folder, kind, context, parent)

    def _launch(self, profile_id, folder, kind, context, parent, exported_surface=None):
        try:
            command = _creation_command(profile_id, folder, kind, context)
            process = Gio.Subprocess.new(command, Gio.SubprocessFlags.STDOUT_PIPE)
        except (OSError, GLib.Error) as error:
            if exported_surface:
                exported_surface.unexport_handle()
            _notify(tr("google_file_create_failed"), str(error))
            return
        stream = Gio.DataInputStream.new(process.get_stdout_pipe())
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self._read_result, parent)
        process.wait_async(None, self._child_finished, exported_surface)

    def _read_result(self, stream, result, parent):
        try:
            line, _length = stream.read_line_finish(result)
            if not line:
                return
            data = json.loads(line.decode("utf-8"))
            if data.get("local_path"):
                _refresh_nautilus(parent, data["local_path"])
        except (GLib.Error, ValueError, AttributeError):
            pass
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self._read_result, parent)

    def _child_finished(self, process, result, surface):
        try:
            process.wait_finish(result)
            if not process.get_successful():
                _notify(tr("google_file_create_failed"), tr("google_create_launch_failed"))
        except GLib.Error:
            pass
        finally:
            if surface:
                surface.unexport_handle()


def _package_root() -> Path:
    for root in _roots():
        for candidate in (root / "src", root):
            if (candidate / "talaryn/google_create_dialog.py").is_file():
                return candidate
    raise FileNotFoundError(tr("google_create_launch_failed"))


def _creation_command(profile_id, folder, kind, context) -> list[str]:
    root = _package_root()
    bootstrap = (
        "import sys;"
        f"sys.path.insert(0, {str(root)!r});"
        "from talaryn.main import main;main()"
    )
    return [sys.executable, "-I", "-c", bootstrap, "google-create", profile_id,
            str(folder), kind, json.dumps(context)]


def _capture_pointer():
    try:
        path = _package_root() / "talaryn/window_position.py"
        spec = importlib.util.spec_from_file_location("talaryn_window_position", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.capture_x11_pointer()
    except (OSError, ImportError, AttributeError):
        return None


def _refresh_nautilus(parent, local_path):
    # Reload the originating window without changing its location or stealing focus.
    # Nautilus 4 does not expose a public directory-reload method to extensions.
    if parent is not None:
        try:
            for action in ("win.reload", "slot.reload", "view.reload"):
                if parent.activate_action(action, None):
                    return
        except (AttributeError, RuntimeError):
            pass
    # Public fallback for Nautilus versions without the reload action, or no parent.
    try:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        connection.call("org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
                        "org.freedesktop.FileManager1", "ShowItems",
                        GLib.Variant("(ass)", ([Path(local_path).as_uri()], "")),
                        None, Gio.DBusCallFlags.NONE, 5000, None, None)
    except (GLib.Error, ValueError):
        pass
