#!/usr/bin/env python3
#
# Copyright (c) 2026 Kamil Krakowski
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

_extension_dir = str(Path(__file__).resolve().parent)
if _extension_dir not in sys.path:
    sys.path.insert(0, _extension_dir)
from talaryn_i18n import tr as _tr

import gi

gi.require_version("Nautilus", "4.0")

from gi.repository import Gio, GLib, GObject, Nautilus


LINK_SUFFIX = ".link.html"
READ_BYTES = 8192
READ_TIMEOUT_SECONDS = 3
MAX_PENDING_READS = 8
MAX_QUEUED_READS = 1024
CONTENT_CACHE_SECONDS = 300
READ_ERROR_RETRY_SECONDS = 10
CONTENT_CACHE_LIMIT = 4096

_google_profiles_cache: tuple[tuple[str, dict, Path], ...] = ()
_google_mounts_cache: tuple[Path, ...] = ()
_profiles_stamp: tuple[int, int] | None = None

MARKERS = [
    {
        "marker": "docs.google.com/document/",
        "emblem": "talaryn-document",
        "label": "Google Docs",
    },
    {
        "marker": "docs.google.com/spreadsheets/",
        "emblem": "talaryn-spreadsheet",
        "label": "Google Sheets",
    },
    {
        "marker": "docs.google.com/presentation/",
        "emblem": "talaryn-presentation",
        "label": "Google Slides",
    },
]


def _config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "talaryn"


def _absolute_path(value: str) -> Path:
    return Path(os.path.abspath(os.path.expandvars(os.path.expanduser(value))))


def _load_google_profiles() -> tuple[tuple[str, dict, Path], ...]:
    global _google_profiles_cache, _google_mounts_cache, _profiles_stamp

    path = _config_dir() / "profiles.json"
    try:
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        _google_profiles_cache = ()
        _google_mounts_cache = ()
        _profiles_stamp = None
        return ()

    if stamp == _profiles_stamp:
        return _google_profiles_cache

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        _google_profiles_cache = ()
        _google_mounts_cache = ()
        _profiles_stamp = stamp
        return ()

    profiles = data.get("profiles", {}) if isinstance(data, dict) else {}
    matches: list[tuple[str, dict, Path]] = []
    for profile_id, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        if str(profile.get("kind", "")) != "gdrive":
            continue
        mount_dir = str(profile.get("mount_dir", "")).strip()
        if mount_dir:
            matches.append(
                (str(profile_id), profile, _absolute_path(mount_dir))
            )
    _google_profiles_cache = tuple(matches)
    _google_mounts_cache = tuple(item[2] for item in matches)
    _profiles_stamp = stamp
    return _google_profiles_cache


def _load_google_mounts() -> tuple[Path, ...]:
    _load_google_profiles()
    return _google_mounts_cache


def _profile_for_path(path: Path):
    for profile_id, profile, mount in _load_google_profiles():
        try:
            path.relative_to(mount)
        except ValueError:
            continue
        return profile_id, profile, mount
    return None


def _talaryn_command() -> list[str] | None:
    executable = shutil.which("talaryn")
    if executable:
        return [executable]
    user_executable = Path.home() / ".local" / "bin" / "talaryn"
    if user_executable.is_file():
        return [str(user_executable)]
    system_executable = Path("/usr/bin/talaryn")
    if system_executable.is_file():
        return [str(system_executable)]
    return None


def _notify_error(message: str) -> None:
    try:
        subprocess.Popen(
            ["notify-send", "Talaryn", message],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError:
        pass


class GoogleLinkEmblemsProvider(
    GObject.GObject,
    Nautilus.InfoProvider,
    Nautilus.MenuProvider,
):
    def __init__(self):
        super().__init__()
        self._content_cache: dict[str, tuple[float, dict | None]] = {}
        self._pending: dict[str, dict] = {}
        self._queued: dict[str, Path] = {}

    def update_file_info(self, file):
        if file.is_directory():
            return

        name = file.get_name() or ""
        if not name.endswith(LINK_SUFFIX):
            return

        path = self._file_to_path(file)
        if path is None or not self._is_inside_google_mount(path):
            return

        try:
            uri = file.get_uri()
        except Exception:
            return
        if not uri:
            return

        cached = self._content_cache.get(uri)
        if cached is not None:
            expires_at, marker = cached
            if time.monotonic() <= expires_at:
                self._apply_marker(file, marker)
                return
            self._content_cache.pop(uri, None)

        self._queue_read(uri, path)

    def _file_to_path(self, file):
        location = file.get_location()
        if location is None:
            return None

        path = location.get_path()
        if path is None:
            return None

        return _absolute_path(path)

    def _is_inside_google_mount(self, path: Path) -> bool:
        return _profile_for_path(path) is not None

    def get_file_items(self, files):
        if len(files) != 1:
            return []
        file = files[0]
        if file.is_directory():
            return []
        name = file.get_name() or ""
        if not name.casefold().endswith(LINK_SUFFIX):
            return []
        path = self._file_to_path(file)
        if path is None:
            return []
        match = _profile_for_path(path)
        if match is None:
            return []
        profile_id, _profile, _mount = match
        item = Nautilus.MenuItem(
            name=f"GoogleLinkEmblemsProvider::{profile_id}::export",
            label=_tr("google_export_action"),
            tip=_tr("google_export_tip"),
        )
        item.connect("activate", self._on_export, profile_id, path)
        return [item]

    def get_background_items(self, _folder):
        return []

    def _on_export(self, _item, profile_id: str, path: Path) -> None:
        command = _talaryn_command()
        if command is None:
            _notify_error(_tr("google_export_launch_failed"))
            return
        try:
            subprocess.Popen(
                [*command, "google-export", profile_id, str(path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        except OSError:
            _notify_error(_tr("google_export_launch_failed"))

    def _apply_marker(self, file, marker: dict | None) -> None:
        if marker is None:
            return
        file.add_emblem(marker["emblem"])
        file.add_string_attribute("Google", marker["label"])

    def _marker_for_content(self, content: str) -> dict | None:
        for item in MARKERS:
            if item["marker"] in content:
                return item
        return None

    def _queue_read(self, uri: str, path: Path) -> None:
        if uri in self._pending or uri in self._queued:
            return
        if len(self._pending) < MAX_PENDING_READS:
            self._start_read(uri, path)
            return
        if len(self._queued) >= MAX_QUEUED_READS:
            self._queued.pop(next(iter(self._queued)))
        self._queued[uri] = path

    def _start_read(self, uri: str, path: Path) -> None:
        cancellable = Gio.Cancellable()
        timeout_id = GLib.timeout_add_seconds(
            READ_TIMEOUT_SECONDS,
            self._cancel_read,
            uri,
        )
        self._pending[uri] = {
            "cancellable": cancellable,
            "timeout_id": timeout_id,
            "stream": None,
        }
        try:
            Gio.File.new_for_path(str(path)).read_async(
                GLib.PRIORITY_DEFAULT,
                cancellable,
                self._stream_opened,
                uri,
            )
        except Exception:
            self._finish_pending(uri, None, READ_ERROR_RETRY_SECONDS)

    def _cancel_read(self, uri: str):
        pending = self._pending.get(uri)
        if pending is not None:
            pending["timeout_id"] = 0
            try:
                pending["cancellable"].cancel()
            except Exception:
                pass
        return GLib.SOURCE_REMOVE

    def _stream_opened(self, source, result, uri: str) -> None:
        pending = self._pending.get(uri)
        if pending is None:
            return
        try:
            stream = source.read_finish(result)
            pending["stream"] = stream
            stream.read_bytes_async(
                READ_BYTES,
                GLib.PRIORITY_DEFAULT,
                pending["cancellable"],
                self._bytes_read,
                uri,
            )
        except Exception:
            self._finish_pending(uri, None, READ_ERROR_RETRY_SECONDS)

    def _bytes_read(self, stream, result, uri: str) -> None:
        marker = None
        cache_seconds = READ_ERROR_RETRY_SECONDS
        try:
            contents = stream.read_bytes_finish(result)
            text = bytes(contents.get_data()).decode(
                "utf-8",
                errors="ignore",
            )
            marker = self._marker_for_content(text)
            cache_seconds = CONTENT_CACHE_SECONDS
        except Exception:
            pass
        self._finish_pending(uri, marker, cache_seconds)

    def _finish_pending(
        self,
        uri: str,
        marker: dict | None,
        cache_seconds: int,
    ) -> None:
        pending = self._pending.pop(uri, None)
        if pending is not None:
            stream = pending.get("stream")
            if stream is not None:
                try:
                    stream.close_async(
                        GLib.PRIORITY_DEFAULT,
                        None,
                        None,
                        None,
                    )
                except Exception:
                    pass
            try:
                timeout_id = int(pending["timeout_id"])
                if timeout_id > 0:
                    GLib.source_remove(timeout_id)
            except Exception:
                pass
        if len(self._content_cache) >= CONTENT_CACHE_LIMIT:
            self._content_cache.pop(next(iter(self._content_cache)))
        self._content_cache[uri] = (
            time.monotonic() + cache_seconds,
            marker,
        )
        try:
            file = Nautilus.FileInfo.lookup_for_uri(uri)
            if file is not None:
                file.invalidate_extension_info()
        except Exception:
            pass
        self._start_next_read()

    def _start_next_read(self) -> None:
        while self._queued and len(self._pending) < MAX_PENDING_READS:
            uri, path = self._queued.popitem()
            try:
                if Nautilus.FileInfo.lookup_for_uri(uri) is None:
                    continue
            except Exception:
                continue
            self._start_read(uri, path)
