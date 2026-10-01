from __future__ import annotations

import http.client
import json
import socket
import subprocess
from pathlib import Path
from typing import Any

from .paths import runtime_dir
from .util import safe_id


MIN_RCLONE_VERSION = (1, 74, 4)
DEFAULT_TIMEOUT_SECONDS = 1.5


def rc_socket_for(profile_id: str) -> Path:
    return runtime_dir() / f"{safe_id(profile_id)}.sock"


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = str(socket_path)

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(self.socket_path)
        self.sock = connection


def rc_call(
    profile_id: str,
    method: str,
    params: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    socket_path = rc_socket_for(profile_id)
    if not socket_path.exists():
        return None

    connection = UnixHTTPConnection(socket_path, timeout)
    body = json.dumps(params or {}, ensure_ascii=False).encode("utf-8")
    try:
        connection.request(
            "POST",
            f"/{method.lstrip('/')}",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        data = response.read()
        if response.status < 200 or response.status >= 300:
            return None
        result = json.loads(data.decode("utf-8"))
    except (OSError, TimeoutError, ValueError, http.client.HTTPException):
        return None
    finally:
        connection.close()
    return result if isinstance(result, dict) else None


def version_is_supported(version: tuple[int, int, int] | None) -> bool:
    return version is not None and version >= MIN_RCLONE_VERSION


def installed_rclone_version() -> tuple[int, int, int] | None:
    try:
        process = subprocess.run(
            ["rclone", "version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if process.returncode != 0:
        return None
    first_line = process.stdout.splitlines()[0] if process.stdout else ""
    value = first_line.removeprefix("rclone v").split("-", 1)[0]
    parts = value.split(".")
    if len(parts) < 3:
        return None
    try:
        return tuple(int(part) for part in parts[:3])
    except ValueError:
        return None


def queue_set_expiry(
    profile_id: str,
    queue_id: int,
    expiry: float,
) -> bool:
    return (
        rc_call(
            profile_id,
            "vfs/queue-set-expiry",
            {"id": queue_id, "expiry": expiry},
        )
        is not None
    )


def set_bwlimit(profile_id: str, rate: str) -> bool:
    return rc_call(profile_id, "core/bwlimit", {"rate": rate}) is not None
