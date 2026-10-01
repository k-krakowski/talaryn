#!/usr/bin/env python3
#
# Copyright (c) 2026 Kamil Krakowski
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
import os
from pathlib import Path
import time

import gi

gi.require_version("Nautilus", "4.0")

from gi.repository import GLib, GObject, Nautilus


APP = "talaryn"
LINK_SUFFIX = ".link.html"
STATUS_SCHEMA = 1
STATUS_POLL_SECONDS = 1
SNAPSHOT_MAX_AGE_SECONDS = 12
TRACKED_FILES_LIMIT = 16384
LOCAL_FILE_CHECK_SECONDS = 0.25

_profiles_cache: dict[str, dict] = {}
_profiles_stamp: tuple[int, int] | None = None
_profiles_checked_at = 0.0
_profile_roots_cache: tuple[tuple[str, dict, Path], ...] = ()
_profile_roots_stamp: tuple[int, int] | None = None
_snapshot_cache: dict = {}
_snapshot_stamp: tuple[int, int] | None = None
_snapshot_syncing_paths: dict[str, frozenset[str] | None] | None = None
_snapshot_checked_at = 0.0


def _config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP


def _cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / APP


def _load_snapshot(force: bool = False) -> dict:
    global _snapshot_cache, _snapshot_stamp, _snapshot_syncing_paths
    global _snapshot_checked_at
    now = time.monotonic()
    if (
        not force
        and now - _snapshot_checked_at < LOCAL_FILE_CHECK_SECONDS
    ):
        if _snapshot_is_fresh(_snapshot_cache):
            return _snapshot_cache
        _snapshot_cache = {}
        _snapshot_syncing_paths = None
        return {}
    _snapshot_checked_at = now
    path = _cache_dir() / "nautilus-status.json"
    try:
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        _snapshot_cache = {}
        _snapshot_stamp = None
        _snapshot_syncing_paths = None
        return {}
    if stamp == _snapshot_stamp:
        if _snapshot_is_fresh(_snapshot_cache):
            return _snapshot_cache
        _snapshot_cache = {}
        _snapshot_syncing_paths = None
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        _snapshot_stamp = stamp
        if _snapshot_is_fresh(_snapshot_cache):
            return _snapshot_cache
        _snapshot_cache = {}
        _snapshot_syncing_paths = None
        return {}
    if not isinstance(data, dict):
        _snapshot_cache = {}
        _snapshot_stamp = stamp
        _snapshot_syncing_paths = None
        return {}
    try:
        schema = int(data.get("schema", 0) or 0)
    except (TypeError, ValueError):
        schema = 0
    if schema != STATUS_SCHEMA:
        _snapshot_cache = {}
        _snapshot_stamp = stamp
        _snapshot_syncing_paths = None
        return {}
    if not _snapshot_is_fresh(data):
        _snapshot_cache = {}
        _snapshot_stamp = stamp
        _snapshot_syncing_paths = None
        return {}
    _snapshot_cache = data
    _snapshot_stamp = stamp
    _snapshot_syncing_paths = _build_syncing_path_index(data)
    return data


def _snapshot_is_fresh(snapshot: dict) -> bool:
    try:
        updated_at = float(snapshot.get("updated_at", 0) or 0)
    except (AttributeError, TypeError, ValueError):
        return False
    return (
        updated_at > 0
        and time.time() - updated_at <= SNAPSHOT_MAX_AGE_SECONDS
    )


def _build_syncing_path_index(
    snapshot: dict,
) -> dict[str, frozenset[str] | None] | None:
    paths = snapshot.get("syncing_paths")
    if not isinstance(paths, dict):
        return None

    index: dict[str, frozenset[str] | None] = {}
    for profile_id, profile_paths in paths.items():
        key = str(profile_id)
        if not isinstance(profile_paths, list):
            index[key] = None
            continue
        index[key] = frozenset(
            str(path).strip("/") for path in profile_paths
        )
    return index


def _load_profiles(force: bool = False) -> dict[str, dict]:
    global _profiles_cache, _profiles_stamp, _profiles_checked_at

    now = time.monotonic()
    if (
        not force
        and now - _profiles_checked_at < LOCAL_FILE_CHECK_SECONDS
    ):
        return _profiles_cache
    _profiles_checked_at = now

    path = _config_dir() / "profiles.json"
    try:
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        _profiles_cache = {}
        _profiles_stamp = None
        return {}

    if stamp == _profiles_stamp:
        return _profiles_cache

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        # Keep the last valid configuration for this exact file generation,
        # without reparsing the same broken contents for every visible file.
        _profiles_stamp = stamp
        return _profiles_cache

    profiles = data.get("profiles", {}) if isinstance(data, dict) else {}
    _profiles_cache = {
        str(profile_id): profile
        for profile_id, profile in profiles.items()
        if isinstance(profile, dict)
    }
    _profiles_stamp = stamp
    return _profiles_cache


def _absolute_path(value: str) -> Path:
    return Path(os.path.abspath(os.path.expandvars(os.path.expanduser(value))))


def _profile_roots() -> tuple[tuple[str, dict, Path], ...]:
    """Return configured roots without touching any FUSE mount.

    Nautilus invokes an InfoProvider in its own process.  Even a seemingly
    harmless stat/ismount call below an rclone mount can wait for the remote
    backend and make the whole file manager appear to be hung.
    """
    global _profile_roots_cache, _profile_roots_stamp

    profiles = _load_profiles()
    if _profiles_stamp == _profile_roots_stamp:
        return _profile_roots_cache

    roots: list[tuple[str, dict, Path]] = []
    for profile_id, profile in profiles.items():
        mount_value = str(profile.get("mount_dir", "")).strip()
        if not mount_value:
            continue
        mount_dir = _absolute_path(mount_value)
        roots.append((profile_id, profile, mount_dir))

    roots.sort(key=lambda item: len(item[2].parts), reverse=True)
    _profile_roots_cache = tuple(roots)
    _profile_roots_stamp = _profiles_stamp
    return _profile_roots_cache


def _profile_for_path(path: Path):
    for profile_id, profile, mount_dir in _profile_roots():
        try:
            relative_path = path.relative_to(mount_dir)
        except ValueError:
            continue
        if relative_path.parts:
            return profile_id, profile, relative_path
    return None


def _snapshot_sync_state(
    profile_id: str,
    relative_path: Path,
) -> bool | None:
    snapshot = _load_snapshot()
    return _sync_state_from_snapshot(
        snapshot,
        _snapshot_syncing_paths,
        profile_id,
        relative_path,
    )


def _sync_state_from_snapshot(
    snapshot: dict,
    syncing_paths: dict[str, frozenset[str] | None] | None,
    profile_id: str,
    relative_path: Path,
) -> bool | None:
    profiles = snapshot.get("profiles")
    if not isinstance(profiles, dict):
        return None
    profile_state = profiles.get(profile_id)
    if not isinstance(profile_state, dict) or not profile_state.get("connected"):
        return None
    if syncing_paths is None:
        return None
    profile_paths = syncing_paths.get(profile_id, frozenset())
    if profile_paths is None:
        return None
    relative = relative_path.as_posix().strip("/")
    return relative in profile_paths


def _status_token(
    snapshot: dict,
    syncing_paths: dict[str, frozenset[str] | None] | None,
) -> tuple | None:
    profiles = snapshot.get("profiles")
    if not isinstance(profiles, dict) or syncing_paths is None:
        return None
    connected = tuple(
        sorted(
            (str(profile_id), bool(state.get("connected")))
            for profile_id, state in profiles.items()
            if isinstance(state, dict)
        )
    )
    paths = tuple(
        sorted(
            (
                str(profile_id),
                None if values is None else tuple(sorted(values)),
            )
            for profile_id, values in syncing_paths.items()
        )
    )
    return connected, paths


def _is_google_link(profile: dict, path: Path) -> bool:
    return (
        str(profile.get("kind", "")) == "gdrive"
        and path.name.lower().endswith(LINK_SUFFIX)
    )


class RcloneSyncEmblemsProvider(GObject.GObject, Nautilus.InfoProvider):
    def __init__(self):
        super().__init__()
        self._tracked_files: dict[str, dict] = {}
        _load_profiles(force=True)
        self._seen_profiles_stamp = _profiles_stamp
        snapshot = _load_snapshot(force=True)
        self._seen_status_token = _status_token(
            snapshot,
            _snapshot_syncing_paths,
        )
        GLib.timeout_add_seconds(
            STATUS_POLL_SECONDS,
            self._poll_status,
        )

    def update_file_info(self, file):
        if file.is_directory():
            return

        location = file.get_location()
        if location is None:
            return
        path_value = location.get_path()
        if not path_value:
            return

        path = _absolute_path(path_value)
        try:
            uri = file.get_uri()
        except Exception:
            uri = ""
        found = _profile_for_path(path)
        if found is None:
            if uri:
                self._tracked_files.pop(uri, None)
            return

        profile_id, profile, relative_path = found
        if _is_google_link(profile, path):
            if uri:
                self._tracked_files.pop(uri, None)
            return

        syncing = _snapshot_sync_state(
            profile_id,
            relative_path,
        )
        if uri:
            self._track_file(
                uri,
                profile_id,
                relative_path,
                syncing,
            )
        if syncing is None:
            return
        if syncing:
            file.add_emblem("rclone-syncing")
            file.add_string_attribute(
                "rclone-sync-status",
                "Synchronizing",
            )
        else:
            file.add_emblem("rclone-synced")
            file.add_string_attribute(
                "rclone-sync-status",
                "Synchronized",
            )

    def _track_file(
        self,
        uri: str,
        profile_id: str,
        relative_path: Path,
        state: bool | None,
    ) -> None:
        if (
            uri not in self._tracked_files
            and len(self._tracked_files) >= TRACKED_FILES_LIMIT
        ):
            self._tracked_files.pop(next(iter(self._tracked_files)))

        # Reinsert an existing URI so the bounded dictionary behaves like a
        # small LRU of recently displayed Nautilus items.
        self._tracked_files.pop(uri, None)
        self._tracked_files[uri] = {
            "profile_id": profile_id,
            "relative_path": relative_path,
            "state": state,
        }

    def _poll_status(self):
        _load_profiles(force=True)
        profiles_changed = _profiles_stamp != self._seen_profiles_stamp
        self._seen_profiles_stamp = _profiles_stamp
        snapshot = _load_snapshot(force=True)
        syncing_paths = _snapshot_syncing_paths
        status_token = _status_token(snapshot, syncing_paths)
        status_changed = status_token != self._seen_status_token
        self._seen_status_token = status_token
        if not profiles_changed and not status_changed:
            return GLib.SOURCE_CONTINUE

        for uri, tracked in list(self._tracked_files.items()):
            if profiles_changed:
                new_state = None
            else:
                new_state = _sync_state_from_snapshot(
                    snapshot,
                    syncing_paths,
                    str(tracked["profile_id"]),
                    tracked["relative_path"],
                )
            if not profiles_changed and new_state == tracked["state"]:
                continue

            tracked["state"] = new_state
            try:
                file = Nautilus.FileInfo.lookup_for_uri(uri)
                if file is None:
                    self._tracked_files.pop(uri, None)
                    continue
                file.invalidate_extension_info()
            except Exception:
                self._tracked_files.pop(uri, None)

        return GLib.SOURCE_CONTINUE
