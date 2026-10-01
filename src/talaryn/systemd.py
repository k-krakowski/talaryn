from __future__ import annotations

import subprocess

from .paths import APP


def unit_name(profile_id: str) -> str:
    return f"{APP}@{profile_id}.service"


def systemctl(action: str, profile_id: str) -> int:
    try:
        return subprocess.run(
            ["systemctl", "--user", action, unit_name(profile_id)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
        ).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 124


def start(profile_id: str) -> bool:
    return systemctl("start", profile_id) == 0


def stop(profile_id: str) -> bool:
    return systemctl("stop", profile_id) == 0


def is_active(profile_id: str) -> bool:
    return systemctl("is-active", profile_id) == 0
