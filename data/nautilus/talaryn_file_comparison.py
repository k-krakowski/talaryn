#!/usr/bin/env python3
#
# Copyright (c) 2026 Kamil Krakowski
# SPDX-License-Identifier: MIT

"""Nautilus actions for one shared Talaryn comparison pool."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

# Isolated workers also load the helper from this installed extension directory.
_extension_dir = str(Path(__file__).resolve().parent)
if _extension_dir not in sys.path:
    sys.path.insert(0, _extension_dir)
from talaryn_i18n import tr


import gi

gi.require_version("Nautilus", "4.0")

from gi.repository import GObject, Nautilus


APP = "talaryn"
SELECTION_SCHEMA = 1


def _config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP


def _selection_file() -> Path:
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache_home / APP / "file-comparison.json"


def _normalized_paths(paths) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in paths:
        path = os.path.abspath(os.path.expanduser(str(value)))
        if path not in seen:
            seen.add(path)
            result.append(path)
    return result


def _settings() -> dict:
    try:
        data = json.loads((_config_dir() / "settings.json").read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _enabled() -> bool:
    return bool(_settings().get("context_file_comparison", False))


def _load_marked_paths() -> list[str]:
    path = _selection_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return []
    paths = data.get("paths", []) if isinstance(data, dict) else []
    return _normalized_paths(paths) if isinstance(paths, list) else []


def _save_marked_paths(paths) -> list[str]:
    normalized = _normalized_paths(paths)
    path = _selection_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    lock_path = path.with_name(f".{path.name}.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        temporary_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(temporary_descriptor, 0o600)
            with os.fdopen(temporary_descriptor, "w", encoding="utf-8") as stream:
                json.dump(
                    {"schema": SELECTION_SCHEMA, "paths": normalized},
                    stream,
                    ensure_ascii=False,
                    indent=2,
                )
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return normalized


def _mark_paths(paths) -> list[str]:
    return _save_marked_paths([*_load_marked_paths(), *paths])


def _unmark_paths(paths) -> list[str]:
    remove = set(_normalized_paths(paths))
    return _save_marked_paths(path for path in _load_marked_paths() if path not in remove)


def _invalidate_files(files) -> None:
    """Ask Nautilus to rebuild extension-provided state for changed files."""
    for file in files:
        try:
            file.invalidate_extension_info()
        except (AttributeError, RuntimeError):
            pass


def _talaryn_command() -> list[str] | None:
    executable = shutil.which(APP)
    if executable:
        return [executable]
    for candidate in (
        Path.home() / ".local" / "bin" / APP,
        Path("/usr/bin") / APP,
    ):
        if candidate.is_file():
            return [str(candidate)]
    return None


class TalarynFileComparisonProvider(GObject.GObject, Nautilus.MenuProvider):
    def get_background_items(self, _current_folder):
        return []

    def get_file_items(self, files):
        if not _enabled():
            return []
        paths = self._file_paths(files)
        if not paths:
            return []

        marked = set(_load_marked_paths())
        selected_marked = [path for path in paths if path in marked]
        items = []
        compare_item = Nautilus.MenuItem(
            name="TalarynFileComparisonProvider::compare",
            label=tr("nautilus_compare"),
            tip=tr("nautilus_compare_help"),
        )
        compare_item.connect("activate", self._compare, paths, files)
        items.append(compare_item)
        if selected_marked:
            item = Nautilus.MenuItem(
                name="TalarynFileComparisonProvider::unmark",
                label=tr("comparison_remove_marked"),
                tip=tr("nautilus_unmark_help"),
            )
            item.connect("activate", self._unmark, selected_marked, files)
            items.append(item)
        return items

    @staticmethod
    def _file_paths(files) -> list[str]:
        paths: list[str] = []
        for file in files:
            try:
                location = file.get_location()
                path = location.get_path() if location is not None else None
            except Exception:
                path = None
            if path:
                paths.append(path)
        return _normalized_paths(paths)

    @staticmethod
    def _unmark(_menu_item, paths, files) -> None:
        if not _enabled():
            return
        _unmark_paths(paths)
        _invalidate_files(files)

    @staticmethod
    def _compare(_menu_item, paths, files) -> None:
        if not _enabled():
            return
        _mark_paths(paths)
        _invalidate_files(files)
        command = _talaryn_command()
        if command is None:
            return
        try:
            subprocess.Popen(
                [*command, "compare", *paths],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        except OSError:
            return
