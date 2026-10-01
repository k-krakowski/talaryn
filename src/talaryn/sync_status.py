from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .activity_grouping import (
    activity_group_key,
    activity_group_timestamp as _activity_group_timestamp,
    activity_parent as _activity_parent,
)
from .activity_store import ActivityStore
from .config import list_profiles
from .icons import DEFAULT_REMOTE_ICON_NAME
from .mount import log_file_for
from .paths import NAUTILUS_STATUS_FILE, SYNC_SNAPSHOT_FILE, ensure_runtime_dirs
from .rc import MIN_RCLONE_VERSION, rc_call, version_is_supported
from .util import expand_path
from .transfer_safety import transfer_state


RECENT_EVENT_LIMIT = 250
LOG_TAIL_BYTES = 4 * 1024 * 1024
SNAPSHOT_MAX_AGE_SECONDS = 12
NAUTILUS_STATUS_SCHEMA = 1
LOCAL_DISK_ICON = "drive-harddisk-symbolic"
ABOUT_REFRESH_SECONDS = 300
_ABOUT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_VERSION_CACHE: dict[tuple[str, int], tuple[int, int, int] | None] = {}

_RC_URL_RE = re.compile(
    r"Serving remote control on (?P<url>http://(?:127\.0\.0\.1|localhost):\d+/)"
)
_LOG_LINE_RE = re.compile(
    r"^(?P<timestamp>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})"
    r"\s+\S+\s+:\s+(?P<message>.*)$"
)
_UPLOAD_SUCCEEDED_RE = re.compile(
    r"^(?P<path>.*?):\s+vfs cache: upload succeeded(?:\s|$)"
)
_COPIED_RESULT_RE = re.compile(
    r"^(?P<path>.*?):\s+Copied "
    r"\((?P<result>new|replaced existing)\)(?:\s|$)"
)
_VFS_RENAMED_RE = re.compile(
    r'^(?P<source>.*?):\s+vfs cache: renamed in cache to "(?P<destination>.*)"$'
)
_MOVED_RE = re.compile(
    r"^(?P<source>.*?):\s+Moved \([^)]*\) to: (?P<destination>.*)$"
)
_DELETED_RE = re.compile(r"^(?P<path>.*?):\s+Deleted(?:\s|$)")


def _parse_log_timestamp(value: str) -> float:
    try:
        return datetime.strptime(value, "%Y/%m/%d %H:%M:%S").timestamp()
    except (TypeError, ValueError):
        return 0.0


def _remote_path_parts(profile: dict[str, Any]) -> tuple[str, ...]:
    remote_path = str(profile.get("remote_path", "")).strip()
    if not remote_path:
        remote_spec = str(profile.get("remote_spec", ""))
        if ":" in remote_spec:
            remote_path = remote_spec.split(":", 1)[1]
    return tuple(
        part for part in Path(remote_path.strip("/")).parts if part not in ("", ".")
    )


def _remote_location(profile: dict[str, Any], path: str) -> str:
    remote_spec = str(profile.get("remote_spec", "")).strip()
    clean_path = path.strip().strip("/")
    if not remote_spec:
        return clean_path
    if not clean_path:
        return remote_spec
    if remote_spec.endswith(":") or remote_spec.endswith("/"):
        return f"{remote_spec}{clean_path}"
    return f"{remote_spec}/{clean_path}"


def _normalize_log_path(profile: dict[str, Any], path: str) -> str:
    clean = path.strip().strip("/")
    remote_spec = str(profile.get("remote_spec", ""))
    remote_name = str(profile.get("remote_name", "")).strip().rstrip(":")
    if not remote_name and ":" in remote_spec:
        remote_name = remote_spec.split(":", 1)[0]
    if remote_name:
        match = re.match(
            rf"^{re.escape(remote_name)}(?:\{{[^}}]+\}})?:(?P<path>.*)$",
            clean,
        )
        if match:
            clean = match.group("path").strip("/")
            remote_path = "/".join(_remote_path_parts(profile))
            if clean == remote_path:
                clean = ""
            elif remote_path and clean.startswith(f"{remote_path}/"):
                clean = clean[len(remote_path) + 1 :]
    return clean


def metadata_relative_path(
    metadata_path: Path,
    metadata_root: Path,
    profile: dict[str, Any],
) -> Path | None:
    """Compatibility helper for old cache metadata and existing installations."""
    try:
        parts = metadata_path.relative_to(metadata_root).parts
    except ValueError:
        return None
    if len(parts) < 2:
        return None
    relative_parts = parts[1:]
    remote_parts = _remote_path_parts(profile)
    if remote_parts and relative_parts[: len(remote_parts)] == remote_parts:
        relative_parts = relative_parts[len(remote_parts) :]
    return Path(*relative_parts) if relative_parts else None


def _rc_url(log_text: str) -> str | None:
    """Compatibility parser; new mounts use deterministic Unix sockets."""
    matches = list(_RC_URL_RE.finditer(log_text))
    return matches[-1].group("url") if matches else None


def _same_transfer_path(first: str, second: str) -> bool:
    first = first.strip().strip("/")
    second = second.strip().strip("/")
    if not first or not second:
        return False
    return (
        first == second
        or first.endswith(f"/{second}")
        or second.endswith(f"/{first}")
    )


def activity_display_path(item: dict[str, Any]) -> str:
    """Return a compact mounted path while retaining local_path for actions."""
    mount_dir = str(item.get("mount_dir", "")).strip()
    mount_name = Path(mount_dir).name if mount_dir else ""
    if not mount_name:
        mount_name = str(item.get("profile_id", "")).strip()
    path = str(item.get("path", "")).strip().strip("/")
    if mount_name and path:
        return (Path(mount_name) / path).as_posix()
    return mount_name or path


def group_activity_events(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group observed profile changes by exact parent folder and time bucket."""
    grouped: dict[str, dict[str, Any]] = {}
    for item in events:
        key = activity_group_key(item)
        path = str(item.get("path", "")).strip().strip("/")
        parent = _activity_parent(path)
        mount_dir = Path(str(item.get("mount_dir", "")))
        folder_path = str(mount_dir / parent) if parent else str(mount_dir)
        group = grouped.setdefault(
            key,
            {
                "group_key": key,
                "profile_id": str(item.get("profile_id", "")),
                "profile_label": str(item.get("profile_label", "")),
                "profile_icon": str(
                    item.get("profile_icon", DEFAULT_REMOTE_ICON_NAME)
                ),
                "relative_folder": parent,
                "folder_path": folder_path,
                "display_folder_path": activity_display_path(
                    {**item, "path": parent}
                ),
                "timestamp": _activity_group_timestamp(item),
                "items": [],
            },
        )
        group["items"].append(item)
        group["timestamp"] = max(
            float(group.get("timestamp", 0) or 0),
            _activity_group_timestamp(item),
        )

    state_order = {
        "uploading": 0,
        "retrying": 1,
        "queued": 2,
        "error": 3,
        "completed": 4,
    }
    result: list[dict[str, Any]] = []
    for group in grouped.values():
        items = group["items"]
        items.sort(
            key=lambda item: (
                state_order.get(str(item.get("state", "completed")), 5),
                -float(item.get("timestamp", 0) or 0),
                str(item.get("path", "")),
            )
        )
        active_count = sum(
            1
            for item in items
            if item.get("state") in ("uploading", "retrying")
        )
        queued_count = sum(1 for item in items if item.get("state") == "queued")
        error_count = sum(1 for item in items if item.get("state") == "error")
        completed_count = sum(
            1 for item in items if item.get("state") == "completed"
        )
        operations = {
            str(item.get("operation", "copy"))
            for item in items
        }
        operation = (
            next(iter(operations)) if len(operations) == 1 else "mixed"
        )
        total_size = sum(int(item.get("size", 0) or 0) for item in items)
        transferred_size = sum(
            int(item.get("size", 0) or 0)
            if item.get("state") == "completed"
            else min(
                int(item.get("size", 0) or 0),
                int(item.get("bytes", 0) or 0),
            )
            for item in items
        )
        if total_size > 0:
            progress = 100.0 * transferred_size / total_size
        else:
            percentages = [
                float(item.get("progress", 0) or 0)
                for item in items
                if "progress" in item
            ]
            progress = (
                sum(percentages) / len(percentages) if percentages else 0.0
            )
        group.update(
            {
                "item_count": len(items),
                "active_count": active_count,
                "queued_count": queued_count,
                "completed_count": completed_count,
                "error_count": error_count,
                "operation": operation,
                "total_size": total_size,
                "transferred_size": transferred_size,
                "progress": min(100.0, max(0.0, progress)),
                "is_live": bool(active_count or queued_count),
            }
        )
        result.append(group)

    result.sort(key=_activity_group_order)
    return result


def add_activity_group_headers(
    groups: list[dict[str, Any]],
    headers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add summary-only groups without loading their child activity rows."""
    known_keys = {str(group.get("group_key", "")) for group in groups}
    for header in headers:
        key = str(header.get("group_key", ""))
        representative = header.get("representative")
        if key in known_keys or not isinstance(representative, dict):
            continue
        placeholder = group_activity_events([representative])
        if not placeholder:
            continue
        group = placeholder[0]
        group["items"] = []
        groups.append(group)
        known_keys.add(key)
    groups.sort(key=_activity_group_order)
    return groups


def _activity_group_order(
    group: dict[str, Any],
) -> tuple[int, float, str, str]:
    if int(group.get("active_count", 0) or 0):
        rank = 0
    elif int(group.get("queued_count", 0) or 0):
        rank = 1
    elif int(group.get("error_count", 0) or 0):
        rank = 2
    else:
        rank = 3
    return (
        rank,
        -float(group.get("timestamp", 0) or 0),
        str(group.get("profile_label", "")),
        str(group.get("folder_path", "")),
    )


def merge_activity_group_summaries(
    groups: list[dict[str, Any]],
    summaries: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply complete persisted totals without replacing paginated children."""
    for group in groups:
        summary = summaries.get(str(group.get("group_key", "")))
        if summary is None:
            continue

        live_items = [
            item
            for item in group.get("items", [])
            if str(item.get("state", ""))
            in ("uploading", "retrying", "queued")
        ]
        active_count = int(summary.get("active_count", 0) or 0) + sum(
            1
            for item in live_items
            if item.get("state") in ("uploading", "retrying")
        )
        queued_count = int(summary.get("queued_count", 0) or 0) + sum(
            1 for item in live_items if item.get("state") == "queued"
        )
        completed_count = int(summary.get("completed_count", 0) or 0)
        error_count = int(summary.get("error_count", 0) or 0)
        total_size = int(summary.get("total_size", 0) or 0) + sum(
            int(item.get("size", 0) or 0) for item in live_items
        )
        transferred_size = int(
            summary.get("transferred_size", 0) or 0
        ) + sum(
            min(
                int(item.get("size", 0) or 0),
                int(item.get("bytes", 0) or 0),
            )
            for item in live_items
        )
        operations = {
            str(summary.get("operation", "mixed"))
        } | {
            str(item.get("operation", "copy")) for item in live_items
        }
        operations.discard("")
        operation = (
            next(iter(operations)) if len(operations) == 1 else "mixed"
        )
        progress = (
            100.0 * transferred_size / total_size
            if total_size > 0
            else 0.0
        )
        group.update(
            {
                "item_count": int(summary.get("item_count", 0) or 0)
                + len(live_items),
                "active_count": active_count,
                "queued_count": queued_count,
                "completed_count": completed_count,
                "error_count": error_count,
                "operation": operation,
                "total_size": total_size,
                "transferred_size": transferred_size,
                "progress": min(100.0, max(0.0, progress)),
                "is_live": bool(active_count or queued_count),
                "timestamp": max(
                    float(group.get("timestamp", 0) or 0),
                    float(summary.get("timestamp", 0) or 0),
                ),
            }
        )

    groups.sort(key=_activity_group_order)
    return groups


def _transfer_speed(transfer: dict[str, Any]) -> float:
    current = float(transfer.get("speedAvg", 0) or 0)
    return current if current > 0 else float(transfer.get("speed", 0) or 0)


def _active_transfers(core_stats: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not core_stats or not isinstance(core_stats.get("transferring"), list):
        return []
    return [
        item
        for item in core_stats["transferring"]
        if isinstance(item, dict) and item.get("name")
    ]


def _mark_active_items(
    items: list[dict[str, Any]],
    core_stats: dict[str, Any] | None,
    _vfs_stats: dict[str, Any] | None = None,
    _fallback_uploading: int = 0,
) -> None:
    """Attach progress from core/stats to authoritative vfs/queue items."""
    for transfer in _active_transfers(core_stats):
        name = str(transfer.get("name", ""))
        for item in items:
            if not _same_transfer_path(str(item.get("path", "")), name):
                continue
            item["state"] = "uploading"
            item["bytes"] = int(transfer.get("bytes", 0) or 0)
            item["size"] = int(
                transfer.get("size", item.get("size", 0)) or 0
            )
            item["size_known"] = (
                "size" in transfer or bool(item.get("size_known"))
            )
            item["progress"] = float(transfer.get("percentage", 0) or 0)
            item["speed"] = _transfer_speed(transfer)
            eta = transfer.get("eta")
            if isinstance(eta, (int, float)):
                item["eta"] = float(eta)
            break


def _is_internal_path(path: str) -> bool:
    clean = path.strip().strip("/")
    return clean.startswith(".Trash-") or Path(clean).name.startswith(
        ".goutputstream-"
    )


def _is_trash_destination(path: str) -> bool:
    parts = Path(path.strip().strip("/")).parts
    return bool(parts) and parts[0].startswith(".Trash-") and "files" in parts


def _is_same_directory_rename(source: str, destination: str) -> bool:
    source_path = Path(source)
    destination_path = Path(destination)
    return (
        source_path.parent == destination_path.parent
        and source_path.name != destination_path.name
    )


def _event_id(
    profile_id: str,
    operation: str,
    path: str,
    timestamp: float,
) -> str:
    return f"{profile_id}:{operation}:{path.strip('/')}:{int(timestamp)}"


def _history_event(
    profile_id: str,
    profile: dict[str, Any],
    operation: str,
    path: str,
    timestamp: float,
    source: str,
    destination: str,
    local_path: str,
    reveal_path: str,
    source_icon: str | None = None,
    destination_icon: str | None = None,
    *,
    state: str = "completed",
    error: str = "",
    size: int = 0,
    tries: int = 0,
    group_timestamp: float | None = None,
) -> dict[str, Any]:
    relative = Path(path.strip().strip("/"))
    profile_icon = str(profile.get("icon", DEFAULT_REMOTE_ICON_NAME))
    size_known = size > 0
    if not size_known and local_path:
        try:
            local_stat = Path(local_path).stat()
            size = int(local_stat.st_size)
            size_known = True
        except OSError:
            pass
    if source_icon is None:
        source_icon = (
            profile_icon if operation in ("rename", "delete") else LOCAL_DISK_ICON
        )
    if destination_icon is None:
        destination_icon = "" if operation == "delete" else profile_icon
    event = {
        "event_id": _event_id(profile_id, operation, path, timestamp),
        "profile_id": profile_id,
        "profile_label": str(profile.get("label", profile_id)),
        "profile_icon": profile_icon,
        "mount_dir": str(Path(expand_path(str(profile.get("mount_dir", ""))))),
        "path": relative.as_posix(),
        "local_path": local_path,
        "reveal_path": reveal_path,
        "name": relative.name,
        "timestamp": timestamp,
        "state": state,
        "operation": operation,
        "direction": (
            "upload" if operation in ("copy", "modify") else "remote"
        ),
        "source": source,
        "source_icon": source_icon,
        "destination": destination,
        "destination_icon": destination_icon,
        "size": size,
        "size_known": size_known,
        "tries": tries,
        "error": error,
    }
    if group_timestamp is not None and group_timestamp > 0:
        event["group_timestamp"] = group_timestamp
        event["first_seen"] = group_timestamp
    return event


def _recent_events(
    profile_id: str,
    profile: dict[str, Any],
    log_text: str,
) -> list[dict[str, Any]]:
    mount_dir = Path(expand_path(str(profile.get("mount_dir", ""))))
    events: list[dict[str, Any]] = []
    upload_results: dict[str, tuple[str, float]] = {}

    for line in log_text.splitlines():
        line_match = _LOG_LINE_RE.match(line)
        if not line_match:
            continue
        timestamp = _parse_log_timestamp(line_match.group("timestamp"))
        message = line_match.group("message")

        copied_result = _COPIED_RESULT_RE.match(message)
        if copied_result:
            path = _normalize_log_path(profile, copied_result.group("path"))
            if path and not _is_internal_path(path):
                operation = (
                    "modify"
                    if copied_result.group("result") == "replaced existing"
                    else "copy"
                )
                upload_results[path] = (operation, timestamp)
            continue

        upload = _UPLOAD_SUCCEEDED_RE.match(message)
        if upload:
            path = _normalize_log_path(profile, upload.group("path"))
            if path and not _is_internal_path(path):
                local_path = str(mount_dir / path)
                operation = "copy"
                result = upload_results.get(path)
                if (
                    result is not None
                    and 0 <= timestamp - result[1] <= 30
                ):
                    operation = result[0]
                events.append(
                    _history_event(
                        profile_id,
                        profile,
                        operation,
                        path,
                        timestamp,
                        local_path,
                        _remote_location(profile, path),
                        local_path,
                        local_path,
                    )
                )
            continue

        moved = _MOVED_RE.match(message) or _VFS_RENAMED_RE.match(message)
        if moved:
            source_path = _normalize_log_path(profile, moved.group("source"))
            destination_path = _normalize_log_path(
                profile, moved.group("destination")
            )
            if (
                not source_path
                or not destination_path
                or _is_internal_path(source_path)
            ):
                continue
            source_local = str(mount_dir / source_path)
            destination_local = str(mount_dir / destination_path)
            if _is_trash_destination(destination_path):
                operation = "delete"
                display_path = source_path
                local_path = source_local
                reveal_path = str(Path(source_local).parent)
                displayed_destination = ""
            else:
                operation = (
                    "rename"
                    if _is_same_directory_rename(source_path, destination_path)
                    else "copy"
                )
                display_path = destination_path
                local_path = destination_local
                reveal_path = destination_local
                displayed_destination = destination_local
            profile_icon = str(profile.get("icon", DEFAULT_REMOTE_ICON_NAME))
            events.append(
                _history_event(
                    profile_id,
                    profile,
                    operation,
                    display_path,
                    timestamp,
                    source_local,
                    displayed_destination,
                    local_path,
                    reveal_path,
                    source_icon=profile_icon,
                    destination_icon=(
                        "" if operation == "delete" else profile_icon
                    ),
                )
            )
            continue

        deleted = _DELETED_RE.match(message)
        if deleted:
            path = _normalize_log_path(profile, deleted.group("path"))
            if path and not _is_internal_path(path):
                local_path = str(mount_dir / path)
                events.append(
                    _history_event(
                        profile_id,
                        profile,
                        "delete",
                        path,
                        timestamp,
                        local_path,
                        "",
                        local_path,
                        str(Path(local_path).parent),
                    )
                )

    events.sort(key=lambda item: float(item["timestamp"]), reverse=True)
    return events


def _queue_items(
    profile_id: str,
    profile: dict[str, Any],
    queue_result: dict[str, Any] | None,
    now: float,
) -> list[dict[str, Any]]:
    queue = queue_result.get("queue", []) if queue_result else []
    if not isinstance(queue, list):
        return []
    mount_dir = Path(expand_path(str(profile.get("mount_dir", ""))))
    profile_icon = str(profile.get("icon", DEFAULT_REMOTE_ICON_NAME))
    result: list[dict[str, Any]] = []
    for raw in queue:
        if not isinstance(raw, dict):
            continue
        path = str(raw.get("name", "")).strip().strip("/")
        if not path or _is_internal_path(path):
            continue
        try:
            queue_id = int(raw.get("id", 0))
        except (TypeError, ValueError):
            queue_id = 0
        uploading = bool(raw.get("uploading"))
        local_path = str(mount_dir / path)
        result.append(
            {
                "event_id": f"{profile_id}:queue:{queue_id}:{path}",
                "profile_id": profile_id,
                "profile_label": str(profile.get("label", profile_id)),
                "profile_icon": profile_icon,
                "mount_dir": str(mount_dir),
                "path": path,
                "local_path": local_path,
                "reveal_path": local_path,
                "name": Path(path).name,
                "size": int(raw.get("size", 0) or 0),
                "size_known": "size" in raw,
                "bytes": 0,
                "timestamp": now,
                "state": "uploading" if uploading else (
                    "retrying" if int(raw.get("tries", 0) or 0) > 0 else "queued"
                ),
                "operation": "copy",
                "direction": "upload",
                "source": local_path,
                "source_icon": LOCAL_DISK_ICON,
                "destination": _remote_location(profile, path),
                "destination_icon": profile_icon,
                "queue_id": queue_id,
                "tries": int(raw.get("tries", 0) or 0),
                "delay": float(raw.get("delay", 0) or 0),
                "expiry": float(raw.get("expiry", 0) or 0),
            }
        )
    return result


def _normalized_transfer_timestamp(value: Any) -> float:
    try:
        timestamp = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return timestamp / 1000 if timestamp > 10_000_000_000 else timestamp


def _ingest_transferred(
    store: ActivityStore,
    profile_id: str,
    profile: dict[str, Any],
    process_id: int,
    transferred_result: dict[str, Any] | None,
    queue_items: list[dict[str, Any]],
    completed_events: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    transferred = (
        transferred_result.get("transferred", []) if transferred_result else []
    )
    if not isinstance(transferred, list):
        return []
    current_paths = {str(item.get("path", "")) for item in queue_items}
    candidates: list[tuple[dict[str, Any], str, float, str]] = []
    for raw in transferred:
        if not isinstance(raw, dict) or str(raw.get("what", "")) != "transferring":
            continue
        path = str(raw.get("name", "")).strip().strip("/")
        timestamp = _normalized_transfer_timestamp(raw.get("timestamp"))
        if not path or timestamp <= 0:
            continue
        transfer_id = (
            f"{profile_id}:{process_id}:{raw.get('jobid', 0)}:"
            f"{int(timestamp * 1000)}:{path}:transferring"
        )
        candidates.append((raw, path, timestamp, transfer_id))
    new_transfer_ids = store.mark_transfers_seen(
        (transfer_id, timestamp)
        for _raw, _path, timestamp, transfer_id in candidates
    )
    events: list[dict[str, Any]] = []
    completed_events = completed_events or []
    mount_dir = Path(expand_path(str(profile.get("mount_dir", ""))))
    for raw, path, timestamp, transfer_id in candidates:
        if transfer_id not in new_transfer_ids:
            continue
        observed = store.observed_upload(
            profile_id,
            process_id,
            path,
            timestamp,
        )
        if observed is None:
            # A download caused by opening, previewing, or caching a file is
            # intentionally omitted from activity. Its speed is still counted.
            continue
        path = str(observed.get("path", path)).strip().strip("/")
        error = str(raw.get("error", "") or "")
        state = "completed"
        if error:
            state = "retrying" if any(
                _same_transfer_path(path, queued) for queued in current_paths
            ) else "error"
        operation = "copy"
        event_timestamp = timestamp
        if not error:
            matching_events = [
                event
                for event in completed_events
                if str(event.get("operation", "")) in ("copy", "modify")
                and _same_transfer_path(
                    path,
                    str(event.get("path", "")),
                )
                and abs(
                    timestamp - float(event.get("timestamp", 0) or 0)
                ) <= 3
            ]
            if matching_events:
                matching_event = min(
                    matching_events,
                    key=lambda event: abs(
                        timestamp - float(event.get("timestamp", 0) or 0)
                    ),
                )
                operation = str(matching_event["operation"])
                event_timestamp = float(matching_event["timestamp"])
        local_path = str(mount_dir / path)
        events.append(
            _history_event(
                profile_id,
                profile,
                operation,
                path,
                event_timestamp,
                local_path,
                _remote_location(profile, path),
                local_path,
                local_path,
                state=state,
                error=error,
                size=int(raw.get("size", 0) or 0),
                tries=int(observed.get("tries", 0) or 0),
                group_timestamp=float(
                    observed.get(
                        "group_timestamp",
                        observed.get("first_seen", timestamp),
                    )
                    or timestamp
                ),
            )
        )
    return events


def _profile_about(
    profile_id: str,
    profile: dict[str, Any],
    now: float,
) -> dict[str, Any]:
    cached = _ABOUT_CACHE.get(profile_id)
    if cached and now - cached[0] < ABOUT_REFRESH_SECONDS:
        return cached[1]
    remote_spec = str(profile.get("remote_spec", "")).strip()
    result = (
        rc_call(
            profile_id,
            "operations/about",
            {"fs": remote_spec},
            timeout=4.0,
        )
        if remote_spec
        else None
    )
    value = result if isinstance(result, dict) else {}
    _ABOUT_CACHE[profile_id] = (now, value)
    return value


def collect_sync_activity(
    profiles: dict[str, dict[str, Any]] | None = None,
    recent_limit: int = RECENT_EVENT_LIMIT,
    *,
    store: ActivityStore | None = None,
) -> dict[str, Any]:
    profiles = list_profiles() if profiles is None else profiles
    store = store or ActivityStore()
    now = time.time()
    active: list[dict[str, Any]] = []
    queued: list[dict[str, Any]] = []
    profile_status: dict[str, dict[str, Any]] = {}
    upload_speed = 0.0
    download_speed = 0.0
    new_events: list[dict[str, Any]] = []

    for profile_id, profile in profiles.items():
        if not isinstance(profile, dict):
            continue

        pid_result = rc_call(profile_id, "core/pid")
        connected = pid_result is not None
        process_id = int((pid_result or {}).get("pid", 0) or 0)
        version_key = (profile_id, process_id)
        if connected and version_key not in _VERSION_CACHE:
            version_result = rc_call(profile_id, "core/version")
            decomposed = (
                version_result.get("decomposed") if version_result else None
            )
            try:
                _VERSION_CACHE[version_key] = (
                    tuple(int(value) for value in decomposed[:3])
                    if isinstance(decomposed, list) and len(decomposed) >= 3
                    else None
                )
            except (TypeError, ValueError):
                _VERSION_CACHE[version_key] = None
        version = _VERSION_CACHE.get(version_key)
        supported = version_is_supported(version)

        core_stats = rc_call(profile_id, "core/stats") if connected else None
        vfs_stats = rc_call(profile_id, "vfs/stats") if connected else None
        queue_result = rc_call(profile_id, "vfs/queue") if connected else None
        transferred_result = (
            rc_call(profile_id, "core/transferred") if connected else None
        )
        items = _queue_items(profile_id, profile, queue_result, now)
        safety_state = transfer_state(queue_result, vfs_stats, core_stats)
        _mark_active_items(items, core_stats)
        store.observe_queue(profile_id, process_id, items, now)

        profile_active = [
            item for item in items if item["state"] in ("uploading", "retrying")
        ]
        profile_queued = [item for item in items if item["state"] == "queued"]
        active.extend(profile_active)
        queued.extend(profile_queued)

        transfers = _active_transfers(core_stats)
        for transfer in transfers:
            speed = _transfer_speed(transfer)
            if any(
                _same_transfer_path(
                    str(item.get("path", "")),
                    str(transfer.get("name", "")),
                )
                for item in items
            ):
                upload_speed += speed
            else:
                download_speed += speed

        log_text = store.read_new_log(log_file_for(profile_id), LOG_TAIL_BYTES)
        completed_log_events: list[dict[str, Any]] = []
        if log_text:
            completed_log_events = _recent_events(
                profile_id,
                profile,
                log_text,
            )
            for event in completed_log_events:
                if event.get("operation") not in ("copy", "modify"):
                    continue
                observed = store.observed_upload(
                    profile_id,
                    process_id,
                    str(event.get("path", "")),
                    float(event.get("timestamp", 0) or 0),
                )
                if observed is None:
                    continue
                observed_size = int(observed.get("size", 0) or 0)
                if observed_size > 0:
                    event["size"] = observed_size
                    event["size_known"] = True
                    event["bytes"] = observed_size
                group_timestamp = float(
                    observed.get(
                        "group_timestamp",
                        observed.get("first_seen", 0),
                    )
                    or 0
                )
                if group_timestamp > 0:
                    event["group_timestamp"] = group_timestamp
                    event["first_seen"] = group_timestamp
            new_events.extend(completed_log_events)
        new_events.extend(
            _ingest_transferred(
                store,
                profile_id,
                profile,
                process_id,
                transferred_result,
                items,
                completed_log_events,
            )
        )

        disk_cache = (
            vfs_stats.get("diskCache", {})
            if isinstance(vfs_stats, dict)
            else {}
        )
        if not isinstance(disk_cache, dict):
            disk_cache = {}
        metadata_cache = (
            vfs_stats.get("metadataCache", {})
            if isinstance(vfs_stats, dict)
            else {}
        )
        if not isinstance(metadata_cache, dict):
            metadata_cache = {}
        about = _profile_about(profile_id, profile, now) if connected else {}
        profile_status[profile_id] = {
            "connected": connected,
            "version": ".".join(str(value) for value in version) if version else "",
            "version_supported": supported if connected else False,
            "active_count": len(profile_active),
            "uploading_count": sum(
                1 for item in profile_active if item["state"] == "uploading"
            ),
            "retrying_count": sum(
                1 for item in profile_active if item["state"] == "retrying"
            ),
            "downloading_count": sum(
                1
                for transfer in transfers
                if not any(
                    _same_transfer_path(
                        str(item.get("path", "")),
                        str(transfer.get("name", "")),
                    )
                    for item in items
                )
            ),
            "queued_count": len(profile_queued),
            "is_syncing": bool(items) or safety_state == "busy",
            "transfer_state": safety_state,
            "speed": sum(_transfer_speed(item) for item in transfers),
            "errors": int((core_stats or {}).get("errors", 0) or 0),
            "last_error": str((core_stats or {}).get("lastError", "") or ""),
            "fatal_error": bool((core_stats or {}).get("fatalError")),
            "retry_error": bool((core_stats or {}).get("retryError")),
            "cache_bytes": int(disk_cache.get("bytesUsed", 0) or 0),
            "cache_files": int(disk_cache.get("files", 0) or 0),
            "cache_error_files": int(disk_cache.get("erroredFiles", 0) or 0),
            "cache_out_of_space": bool(disk_cache.get("outOfSpace")),
            "metadata_dirs": int(metadata_cache.get("dirs", 0) or 0),
            "metadata_files": int(metadata_cache.get("files", 0) or 0),
            "quota": about,
            "process_id": process_id,
        }

    events_by_id: dict[str, dict[str, Any]] = {}
    for event in new_events:
        event_id = str(event.get("event_id", ""))
        current = events_by_id.get(event_id)
        if current is None:
            events_by_id[event_id] = event
            continue
        current_score = (
            bool(current.get("error")),
            int(current.get("size", 0) or 0),
            int(current.get("tries", 0) or 0),
        )
        event_score = (
            bool(event.get("error")),
            int(event.get("size", 0) or 0),
            int(event.get("tries", 0) or 0),
        )
        if event_score > current_score:
            events_by_id[event_id] = event
    inserted = store.save_events(events_by_id.values())
    total_speed = upload_speed + download_speed
    store.record_speed(now, upload_speed, download_speed)
    recent = store.recent(recent_limit)
    active.sort(key=lambda item: (str(item["profile_label"]), str(item["path"])))
    queued.sort(
        key=lambda item: (
            float(item.get("expiry", 0) or 0),
            str(item["profile_label"]),
            str(item["path"]),
        )
    )
    events = [*active, *queued, *recent]
    syncing_paths: dict[str, list[str]] = {}
    for item in [*active, *queued]:
        syncing_paths.setdefault(str(item["profile_id"]), []).append(
            str(item["path"])
        )
    return {
        "updated_at": now,
        "minimum_rclone_version": ".".join(
            str(value) for value in MIN_RCLONE_VERSION
        ),
        "active": active,
        "downloads": [],
        "queued": queued,
        "recent": recent,
        "events": events,
        "profiles": profile_status,
        "syncing_paths": syncing_paths,
        "active_count": len(active),
        "downloading_count": sum(
            int(status.get("downloading_count", 0))
            for status in profile_status.values()
        ),
        "total_active_count": len(active),
        "queued_count": len(queued),
        "error_count": sum(
            int(status.get("cache_error_files", 0))
            for status in profile_status.values()
        ),
        "is_syncing": any(state["is_syncing"] for state in profile_status.values()),
        "upload_speed": upload_speed,
        "download_speed": download_speed,
        "total_speed": total_speed,
        "speed_history": store.speed_history(),
        "history_summary": store.summary(),
        "history_total": store.activity_count(),
        "new_events": inserted,
    }


def _write_json_snapshot(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _nautilus_status_payload(activity: dict[str, Any]) -> dict[str, Any]:
    profiles = activity.get("profiles", {})
    if not isinstance(profiles, dict):
        profiles = {}
    syncing_paths = activity.get("syncing_paths", {})
    if not isinstance(syncing_paths, dict):
        syncing_paths = {}
    return {
        "schema": NAUTILUS_STATUS_SCHEMA,
        # This timestamp describes the lightweight file itself.  Collection
        # may involve several RC calls, so reusing the collection start time
        # could make a newly written file look stale after a slow backend.
        "updated_at": time.time(),
        "profiles": {
            str(profile_id): {
                "connected": bool(state.get("connected")),
            }
            for profile_id, state in profiles.items()
            if isinstance(state, dict)
        },
        "syncing_paths": syncing_paths,
    }


def write_sync_snapshot(activity: dict[str, Any]) -> None:
    ensure_runtime_dirs()
    payload = dict(activity)
    payload.pop("new_events", None)
    _write_json_snapshot(SYNC_SNAPSHOT_FILE, payload)
    # Nautilus runs extensions in the file manager process.  Keep its status
    # input deliberately small so a busy activity history is never parsed in
    # the UI thread once per refresh.
    _write_json_snapshot(
        NAUTILUS_STATUS_FILE,
        _nautilus_status_payload(activity),
    )


def read_sync_snapshot(
    max_age: float = SNAPSHOT_MAX_AGE_SECONDS,
) -> dict[str, Any] | None:
    try:
        data = json.loads(SYNC_SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        updated_at = float(data.get("updated_at", 0) or 0)
    except (TypeError, ValueError):
        return None
    if not 0 <= time.time() - updated_at <= max_age or updated_at <= 0:
        return None
    return data


def read_nautilus_status_snapshot(
    max_age: float = SNAPSHOT_MAX_AGE_SECONDS,
) -> dict[str, Any] | None:
    try:
        data = json.loads(NAUTILUS_STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        schema = int(data.get("schema", 0) or 0)
        updated_at = float(data.get("updated_at", 0) or 0)
    except (TypeError, ValueError):
        return None
    if schema != NAUTILUS_STATUS_SCHEMA:
        return None
    if updated_at <= 0 or time.time() - updated_at > max_age:
        return None
    return data


def get_sync_activity(
    profiles: dict[str, dict[str, Any]] | None = None,
    recent_limit: int = RECENT_EVENT_LIMIT,
) -> dict[str, Any]:
    snapshot = read_sync_snapshot()
    if snapshot is not None:
        recent = snapshot.get("recent", [])
        if isinstance(recent, list) and len(recent) > recent_limit:
            snapshot = dict(snapshot)
            snapshot["recent"] = recent[:recent_limit]
            active = snapshot.get("active", [])
            queued = snapshot.get("queued", [])
            snapshot["events"] = [*active, *queued, *snapshot["recent"]]
        return snapshot

    # Only the dedicated monitor may call collect_sync_activity(). Running a
    # second collector in GTK would block the UI and race the history ingest.
    profiles = list_profiles() if profiles is None else profiles
    profile_status = {
        profile_id: {
            "connected": False,
            "version": "",
            "version_supported": False,
            "active_count": 0,
            "downloading_count": 0,
            "queued_count": 0,
            "is_syncing": False,
            "transfer_state": "unknown",
            "speed": 0.0,
            "errors": 0,
            "last_error": "",
            "fatal_error": False,
            "cache_bytes": 0,
            "cache_files": 0,
            "cache_error_files": 0,
            "cache_out_of_space": False,
            "quota": {},
        }
        for profile_id in profiles
    }
    empty_summary = {
        "bytes": 0,
        "completed": 0,
        "errors": 0,
        "deleted": 0,
        "renamed": 0,
        "modified": 0,
    }
    return {
        "updated_at": time.time(),
        "monitor_available": False,
        "active": [],
        "downloads": [],
        "queued": [],
        "recent": [],
        "events": [],
        "profiles": profile_status,
        "syncing_paths": {},
        "active_count": 0,
        "downloading_count": 0,
        "total_active_count": 0,
        "queued_count": 0,
        "error_count": 0,
        "is_syncing": False,
        "upload_speed": 0.0,
        "download_speed": 0.0,
        "total_speed": 0.0,
        "speed_history": [],
        "history_summary": {
            "day": dict(empty_summary),
            "month": dict(empty_summary),
        },
        "history_total": 0,
        "new_events": [],
    }
