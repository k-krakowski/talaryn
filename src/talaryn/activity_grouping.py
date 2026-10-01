from __future__ import annotations

from pathlib import Path
from typing import Any


ACTIVITY_GROUP_SECONDS = 5 * 60


def activity_parent(path: str) -> str:
    parent = Path(path.strip().strip("/")).parent.as_posix()
    return "" if parent == "." else parent


def activity_group_timestamp(item: dict[str, Any]) -> float:
    for field in ("group_timestamp", "first_seen", "timestamp"):
        try:
            value = float(item.get(field, 0) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0.0


def activity_group_key(item: dict[str, Any]) -> str:
    profile_id = str(item.get("profile_id", ""))
    parent = activity_parent(str(item.get("path", "")))
    timestamp = activity_group_timestamp(item)
    bucket = (
        int(timestamp // ACTIVITY_GROUP_SECONDS) * ACTIVITY_GROUP_SECONDS
        if timestamp > 0
        else 0
    )
    return f"{profile_id}:{bucket}:{parent}"
