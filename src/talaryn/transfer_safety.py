"""Conservative unmount decisions, independent of activity display filters."""
from __future__ import annotations

import math
import time
from typing import Any

from .rc import rc_call


def transfer_state(queue: Any, vfs: Any, core: Any) -> str:
    """Return busy, idle or unknown. Missing responses never mean idle."""
    if not isinstance(vfs, dict) or not isinstance(core, dict):
        return "unknown"
    transferring = core.get("transferring", [])
    if not isinstance(transferring, list):
        return "unknown"
    if transferring:
        return "busy"
    disk = vfs.get("diskCache")
    if disk is None:
        # vfs/queue is unavailable when the disk cache is disabled.
        options = vfs.get("opt", {})
        if isinstance(options, dict) and options.get("CacheMode") in (0, "off"):
            return "idle"
        return "unknown"
    if not isinstance(disk, dict):
        return "unknown"
    counters = [
        disk.get(key)
        for key in ("uploadsQueued", "uploadsInProgress", "erroredFiles")
    ]
    if any(type(value) is not int or value < 0 for value in counters):
        return "unknown"
    if any(counters):
        return "busy"
    if not isinstance(queue, dict) or not isinstance(queue.get("queue"), list):
        return "unknown"
    return "busy" if queue["queue"] else "idle"


def probe_transfer_state(profile_id: str) -> str:
    """Recheck RC immediately before stopping a mount; never trust old UI data."""
    queue = rc_call(profile_id, "vfs/queue")
    vfs = rc_call(profile_id, "vfs/stats")
    core = rc_call(profile_id, "core/stats")
    return transfer_state(queue, vfs, core)


def snapshot_allows_unmount(activity: dict[str, Any], profile_id: str) -> bool:
    try:
        age = time.time() - float(activity["updated_at"])
        state = activity["profiles"][profile_id]
        return (
            math.isfinite(age) and 0 <= age <= 12
            and state.get("connected") is True
            and state.get("transfer_state") == "idle"
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
