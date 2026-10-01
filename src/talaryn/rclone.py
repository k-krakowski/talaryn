from __future__ import annotations

import configparser
from dataclasses import dataclass
import http.client
import json
import os
import secrets
import subprocess
import threading
import time
from typing import Any

from .icons import (
    DEFAULT_REMOTE_ICON_NAME,
    OBJECT_STORAGE_REMOTE_ICON_NAME,
    WEBDAV_REMOTE_ICON_NAME,
)
from .paths import runtime_dir
from .rc import UnixHTTPConnection
from .util import command_exists, run_capture, safe_id


INVALID_REMOTE_NAME = "invalid rclone remote name"
_RCLONE_DIAGNOSTIC_ENV = {
    "RCLONE_DUMP",
    "RCLONE_DUMP_BODIES",
    "RCLONE_DUMP_HEADERS",
    "RCLONE_LOG_LEVEL",
    "RCLONE_USE_JSON_LOG",
    "RCLONE_VERBOSE",
}


def normalize_remote_name(name: object) -> str | None:
    """Return a CLI-safe rclone remote name, accepting one trailing colon."""
    value = str(name or "").strip()
    if value.endswith(":"):
        value = value[:-1]
    if (
        not value
        or value.startswith("-")
        or any(char in value for char in (":", "/", "\\"))
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return None
    return value


def _valid_remote_type(remote_type: object) -> str | None:
    value = str(remote_type or "").strip()
    if (
        not value
        or value.startswith("-")
        or any(char in value for char in (":", "/", "\\"))
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return None
    return value


@dataclass(frozen=True)
class RcloneCommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.cancelled


@dataclass(frozen=True)
class RemoteConfigStep:
    command_ok: bool
    state: str = ""
    option: dict[str, Any] | None = None
    error: str = ""
    result: str = ""

    @property
    def complete(self) -> bool:
        return self.command_ok and not self.state


class RcloneCommandRunner:
    """Run one cancellable rclone operation at a time."""

    def __init__(self) -> None:
        self._operation_lock = threading.Lock()
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._cancel_requested = False

    def run(
        self,
        command: list[str],
        timeout: float | None = None,
    ) -> RcloneCommandResult:
        with self._operation_lock:
            return self._run_once(command, timeout)

    def _run_once(
        self,
        command: list[str],
        timeout: float | None,
    ) -> RcloneCommandResult:
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            with self._lock:
                cancelled = self._cancel_requested
                self._cancel_requested = False
            return RcloneCommandResult(
                127,
                "",
                str(exc),
                cancelled=cancelled,
            )

        with self._lock:
            self._process = process
            cancel_requested = self._cancel_requested

        if cancel_requested:
            process.terminate()

        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()

        with self._lock:
            cancelled = self._cancel_requested
            self._process = None
            self._cancel_requested = False

        return RcloneCommandResult(
            process.returncode,
            stdout,
            stderr,
            timed_out=timed_out,
            cancelled=cancelled,
        )

    def run_config(
        self,
        method: str,
        payload: dict[str, Any],
        timeout: float = 600.0,
    ) -> RcloneCommandResult:
        """Send configuration secrets in an HTTP body, never process argv."""
        with self._operation_lock:
            return self._run_config_once(method, payload, timeout)

    def _run_config_once(
        self,
        method: str,
        payload: dict[str, Any],
        timeout: float,
    ) -> RcloneCommandResult:
        socket_path = runtime_dir() / (
            f"config-{os.getpid()}-{secrets.token_hex(8)}.sock"
        )
        command = [
            "rclone",
            "rcd",
            "--rc-addr",
            f"unix://{socket_path}",
            "--rc-no-auth",
            "--log-level",
            "ERROR",
        ]
        environment: dict[str, str] = {}
        for key, value in os.environ.items():
            normalized = key.upper()
            if (
                normalized == "RCLONE_RC"
                or normalized.startswith("RCLONE_RC_")
                or normalized in _RCLONE_DIAGNOSTIC_ENV
                or normalized.startswith("RCLONE_DUMP_")
                or normalized.startswith("RCLONE_LOG_FILE")
            ):
                continue
            environment[key] = value
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
            )
        except OSError as exc:
            with self._lock:
                cancelled = self._cancel_requested
                self._cancel_requested = False
            return RcloneCommandResult(
                127,
                "",
                str(exc),
                cancelled=cancelled,
            )

        with self._lock:
            self._process = process
            cancel_requested = self._cancel_requested
        if cancel_requested:
            process.terminate()

        returncode = 1
        stdout = ""
        stderr = ""
        timed_out = False
        deadline = time.monotonic() + 5.0
        connection: UnixHTTPConnection | None = None
        try:
            while not socket_path.exists():
                if process.poll() is not None:
                    stderr = "rclone rcd exited before creating its socket"
                    returncode = process.returncode or 1
                    break
                if time.monotonic() >= deadline:
                    stderr = "rclone rcd did not create its Unix socket"
                    returncode = 124
                    timed_out = True
                    break
                time.sleep(0.05)
            else:
                try:
                    socket_path.chmod(0o600)
                    connection = UnixHTTPConnection(
                        socket_path,
                        timeout=max(1.0, timeout),
                    )
                    body = json.dumps(
                        payload,
                        ensure_ascii=False,
                    ).encode("utf-8")
                    connection.request(
                        "POST",
                        f"/{method.lstrip('/')}",
                        body=body,
                        headers={"Content-Type": "application/json"},
                    )
                    response = connection.getresponse()
                    response_text = response.read().decode(
                        "utf-8",
                        errors="replace",
                    )
                    if 200 <= response.status < 300:
                        returncode = 0
                        stdout = response_text
                    else:
                        returncode = 1
                        stderr = response_text
                except (
                    OSError,
                    TimeoutError,
                    http.client.HTTPException,
                ) as exc:
                    timed_out = isinstance(exc, TimeoutError)
                    returncode = 124 if timed_out else 1
                    stderr = str(exc)
        finally:
            if connection is not None:
                connection.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            with self._lock:
                cancelled = self._cancel_requested
                self._process = None
                self._cancel_requested = False
            try:
                socket_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

        return RcloneCommandResult(
            returncode,
            stdout,
            stderr,
            timed_out=timed_out,
            cancelled=cancelled,
        )

    def cancel(self) -> None:
        with self._lock:
            self._cancel_requested = True
            process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass


def _config_parameters(parameters: dict[str, str]) -> dict[str, str]:
    return {
        str(name): str(value)
        for name, value in parameters.items()
        if str(name) and str(value) != ""
    }


def _parse_remote_config_step(result: RcloneCommandResult) -> RemoteConfigStep:
    if not result.ok:
        error = result.stderr.strip() or result.stdout.strip()
        return RemoteConfigStep(False, error=error)

    text = result.stdout.strip()
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        start = text.find("{")
        end = text.rfind("}")
        try:
            payload = json.loads(text[start : end + 1])
        except (TypeError, ValueError):
            return RemoteConfigStep(
                False,
                error=result.stderr.strip() or text,
            )

    if not isinstance(payload, dict):
        return RemoteConfigStep(False, error=result.stderr.strip() or text)

    option = payload.get("Option")
    return RemoteConfigStep(
        True,
        state=str(payload.get("State", "") or ""),
        option=option if isinstance(option, dict) else None,
        error=str(payload.get("Error", "") or ""),
        result=str(payload.get("Result", "") or ""),
    )


def create_remote_config(
    name: str,
    remote_type: str,
    parameters: dict[str, str],
    runner: RcloneCommandRunner | None = None,
) -> RemoteConfigStep:
    remote_name = normalize_remote_name(name)
    safe_remote_type = _valid_remote_type(remote_type)
    if remote_name is None:
        return RemoteConfigStep(False, error=INVALID_REMOTE_NAME)
    if safe_remote_type is None:
        return RemoteConfigStep(False, error="invalid rclone remote type")
    if not command_exists("rclone"):
        return RemoteConfigStep(False, error="rclone is not installed")

    result = (runner or RcloneCommandRunner()).run_config(
        "config/create",
        {
            "name": remote_name,
            "type": safe_remote_type,
            "parameters": _config_parameters(parameters),
            "opt": {
                "nonInteractive": True,
                "obscure": True,
            },
        },
    )
    return _parse_remote_config_step(result)


def continue_remote_config(
    name: str,
    state: str,
    answer: str,
    parameters: dict[str, str],
    runner: RcloneCommandRunner | None = None,
) -> RemoteConfigStep:
    remote_name = normalize_remote_name(name)
    if remote_name is None:
        return RemoteConfigStep(False, error=INVALID_REMOTE_NAME)
    if not command_exists("rclone"):
        return RemoteConfigStep(False, error="rclone is not installed")

    result = (runner or RcloneCommandRunner()).run_config(
        "config/update",
        {
            "name": remote_name,
            "parameters": _config_parameters(parameters),
            "opt": {
                "continue": True,
                "state": state,
                "result": answer,
                "nonInteractive": True,
                "obscure": True,
            },
        },
    )
    return _parse_remote_config_step(result)


def update_remote_config(
    name: str,
    parameters: dict[str, str],
    runner: RcloneCommandRunner | None = None,
    clear_keys: set[str] | None = None,
) -> RemoteConfigStep:
    remote_name = normalize_remote_name(name)
    if remote_name is None:
        return RemoteConfigStep(False, error=INVALID_REMOTE_NAME)
    if not command_exists("rclone"):
        return RemoteConfigStep(False, error="rclone is not installed")
    if not any(str(value) for value in parameters.values()) and not clear_keys:
        return RemoteConfigStep(False, error="no configuration values supplied")

    config_parameters = _config_parameters(parameters)
    config_parameters.update(
        {
            key: ""
            for key in sorted(clear_keys or set())
            if key and not str(parameters.get(key, ""))
        }
    )
    result = (runner or RcloneCommandRunner()).run_config(
        "config/update",
        {
            "name": remote_name,
            "parameters": config_parameters,
            "opt": {
                "nonInteractive": True,
                "obscure": True,
            },
        },
    )
    return _parse_remote_config_step(result)


def test_remote_connection(
    name: str,
    runner: RcloneCommandRunner | None = None,
    timeout: float = 35,
) -> RcloneCommandResult:
    remote_name = normalize_remote_name(name)
    if remote_name is None:
        return RcloneCommandResult(2, "", INVALID_REMOTE_NAME)
    if not command_exists("rclone"):
        return RcloneCommandResult(127, "", "rclone is not installed")

    command = [
        "rclone",
        "lsd",
        f"{remote_name}:",
        "--max-depth",
        "1",
        "--contimeout",
        "10s",
        "--timeout",
        "20s",
    ]
    return (runner or RcloneCommandRunner()).run(command, timeout=timeout)


def delete_remote_config(name: str) -> tuple[bool, str]:
    remote_name = normalize_remote_name(name)
    if remote_name is None:
        return False, INVALID_REMOTE_NAME
    if not command_exists("rclone"):
        return False, "rclone is not installed"
    code, stdout, stderr = run_capture(
        ["rclone", "config", "delete", remote_name]
    )
    return code == 0, stderr.strip() or stdout.strip()


def remote_name_exists(name: str) -> bool:
    remote_name = normalize_remote_name(name)
    if remote_name is None:
        return False
    expected = remote_name.casefold()
    return any(
        str(remote.get("name", "")).casefold() == expected
        for remote in list_remotes()
    )


def _parse_redacted_config(text: str) -> dict[str, dict[str, str]]:
    parser = configparser.ConfigParser(
        interpolation=None,
        strict=False,
    )
    try:
        parser.read_string(text)
    except configparser.Error:
        return {}

    return {
        section: {
            str(key): str(value)
            for key, value in parser.items(section, raw=True)
        }
        for section in parser.sections()
    }


def redacted_remote_config(name: str) -> tuple[dict[str, str], str]:
    remote_name = normalize_remote_name(name)
    if remote_name is None:
        return {}, INVALID_REMOTE_NAME
    if not command_exists("rclone"):
        return {}, "rclone is not installed"

    code, stdout, stderr = run_capture(
        ["rclone", "config", "redacted", remote_name]
    )
    if code != 0:
        return {}, stderr.strip() or stdout.strip()

    configs = _parse_redacted_config(stdout)
    expected = remote_name.casefold()
    for remote_name, config in configs.items():
        if remote_name.casefold() == expected:
            return config, ""
    return {}, stderr.strip() or f"remote not found: {name}"


def _parse_listremotes(stdout: str) -> list[str]:
    names: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        names.append(line.split()[0].rstrip(":"))
    return names


def _config_dump_details() -> dict[str, dict[str, str]]:
    if not command_exists("rclone"):
        return {}

    code, stdout, _stderr = run_capture(["rclone", "config", "redacted"])
    if code != 0:
        return {}

    result: dict[str, dict[str, str]] = {}
    for name, config in _parse_redacted_config(stdout).items():
        result[str(name)] = {
            str(key): str(value) for key, value in config.items()
        }
    return result


def list_remotes() -> list[dict[str, str]]:
    if not command_exists("rclone"):
        return []

    code, stdout, _stderr = run_capture(["rclone", "listremotes"])
    if code != 0:
        return []

    names = _parse_listremotes(stdout)
    config_map = _config_dump_details()

    remotes: list[dict[str, str]] = []
    for name in names:
        config = config_map.get(name, {})
        rtype = str(config.get("type", ""))
        vendor = str(config.get("vendor", ""))
        remotes.append(
            {
                "id": safe_id(name),
                "name": name,
                "type": rtype,
                "kind": detect_kind(name, rtype, vendor),
            }
        )
    return remotes


def detect_kind(name: str, rtype: str = "", vendor: str = "") -> str:
    lname = name.lower()
    lrtype = rtype.lower()
    lvendor = vendor.lower()

    if lrtype == "google cloud storage":
        return "google cloud storage"
    if lrtype == "drive" or "gdrive" in lname or "google" in lname:
        return "gdrive"
    if lrtype == "onedrive" or "onedrive" in lname:
        return "onedrive"
    if lrtype == "webdav" and lvendor in ("fastmail", "nextcloud"):
        return lvendor
    if lrtype == "sftp" or lname.startswith("edi"):
        return "sftp"
    return lrtype or "generic"


def icon_for_kind(kind: str) -> str:
    icons = {
        "gdrive": DEFAULT_REMOTE_ICON_NAME,
        "drive": DEFAULT_REMOTE_ICON_NAME,
        "onedrive": DEFAULT_REMOTE_ICON_NAME,
        "dropbox": DEFAULT_REMOTE_ICON_NAME,
        "pcloud": DEFAULT_REMOTE_ICON_NAME,
        "box": DEFAULT_REMOTE_ICON_NAME,
        "protondrive": DEFAULT_REMOTE_ICON_NAME,
        "mega": DEFAULT_REMOTE_ICON_NAME,
        "iclouddrive": DEFAULT_REMOTE_ICON_NAME,
        "seafile": DEFAULT_REMOTE_ICON_NAME,
        "nextcloud": WEBDAV_REMOTE_ICON_NAME,
        "fastmail": WEBDAV_REMOTE_ICON_NAME,
        "webdav": WEBDAV_REMOTE_ICON_NAME,
        "smb": "smb.svg",
        "b2": OBJECT_STORAGE_REMOTE_ICON_NAME,
        "s3": OBJECT_STORAGE_REMOTE_ICON_NAME,
        "google cloud storage": OBJECT_STORAGE_REMOTE_ICON_NAME,
        "azureblob": OBJECT_STORAGE_REMOTE_ICON_NAME,
        "azurefiles": OBJECT_STORAGE_REMOTE_ICON_NAME,
        "sftp": "sftp.svg",
        "ftp": "ftp.svg",
    }
    return icons.get(kind, DEFAULT_REMOTE_ICON_NAME)


def build_remote_spec(remote_name: str, remote_path: str) -> str:
    name = remote_name.strip().rstrip(":")
    path = remote_path.strip()
    if not path:
        return f"{name}:"
    return f"{name}:{path}"


def default_custom_command(kind: str) -> str:
    extra_args = ""

    if kind == "gdrive":
        extra_args = "--drive-export-formats link.html "

    return (
        "rclone mount {remote_spec} {mount_dir} "
        "--vfs-cache-mode {vfs_cache_mode} "
        "--vfs-cache-max-size {vfs_cache_max_size} "
        "--vfs-cache-max-age {vfs_cache_max_age} "
        "--dir-cache-time {dir_cache_time} "
        "--umask {umask} "
        f"{extra_args}"
        "--cache-dir {cache_dir} "
        "--log-file {log_file} "
        "--log-level INFO"
    )


def ssh_development_command() -> str:
    return (
        "rclone mount {remote_spec} {mount_dir} "
        "--vfs-cache-mode {vfs_cache_mode} "
        "--vfs-cache-max-size {vfs_cache_max_size} "
        "--vfs-cache-max-age {vfs_cache_max_age} "
        "--dir-cache-time {dir_cache_time} "
        "--vfs-write-back 1s "
        "--vfs-links "
        "--umask {umask} "
        "--cache-dir {cache_dir} "
        "--log-file {log_file} "
        "--log-level INFO"
    )


def sftp_no_hashcheck_command() -> str:
    return f"{default_custom_command('sftp')} --sftp-disable-hashcheck"


def clean_remote_dir_name(name: str) -> str:
    name = name.strip()

    # rclone lsf --dirs-only usually returns directories with a trailing "/".
    while name.endswith("/"):
        name = name[:-1]

    return name


def remote_join(base_path: str, child_name: str) -> str:
    base_path = base_path.strip()
    child_name = clean_remote_dir_name(child_name)

    if not child_name:
        return base_path

    if not base_path:
        return child_name

    if base_path == "/":
        return f"/{child_name}"

    return f"{base_path.rstrip('/')}/{child_name}"


def remote_parent(path: str) -> str:
    path = path.strip().rstrip("/")

    if not path:
        return ""

    if path == "/":
        return ""

    is_absolute = path.startswith("/")

    if "/" not in path.strip("/"):
        return "/" if is_absolute else ""

    parent = path.rsplit("/", 1)[0]

    if not parent and is_absolute:
        return "/"

    return parent


def list_remote_dirs(remote_name: str, remote_path: str) -> tuple[list[str], str]:
    """Return directory names and an empty error, or an empty list and an error."""
    safe_remote_name = normalize_remote_name(remote_name)
    if safe_remote_name is None:
        return [], INVALID_REMOTE_NAME
    if not command_exists("rclone"):
        return [], "rclone is not installed"

    remote_spec = build_remote_spec(safe_remote_name, remote_path)

    code, stdout, stderr = run_capture(
        [
            "rclone",
            "lsf",
            remote_spec,
            "--dirs-only",
            "--format",
            "p",
        ]
    )

    if code != 0:
        return [], stderr.strip() or f"rclone lsf failed for {remote_spec}"

    dirs: list[str] = []

    for line in stdout.splitlines():
        name = clean_remote_dir_name(line)
        if name:
            dirs.append(name)

    dirs.sort(key=str.lower)
    return dirs, ""
