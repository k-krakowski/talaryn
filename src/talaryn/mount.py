from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import get_profile, load_settings
from .icons import DEFAULT_REMOTE_ICON_NAME, remote_icon_path
from .paths import CACHE_DIR, ensure_runtime_dirs
from .rc import rc_socket_for, set_bwlimit
from .util import command_exists, expand_path, run_quiet, shlex_format


def log_file_for(profile_id: str) -> Path:
    return CACHE_DIR / f"{profile_id}.log"

def command_file_for(profile_id: str) -> Path:
    return CACHE_DIR / f"{profile_id}.command.log"

def cache_dir_for(profile_id: str) -> Path:
    return CACHE_DIR / profile_id

def clear_logs_for(profile_id: str) -> bool:
    base_log = log_file_for(profile_id)
    command_log = command_file_for(profile_id)
    paths = [base_log, command_log, command_log.with_name(f"{command_log.name}.1")]
    rotated_log_pattern = re.compile(
        rf"^{re.escape(base_log.stem)}-\d{{4}}-\d{{2}}-\d{{2}}T.+"
        r"\.log(?:\.gz)?$"
    )
    try:
        for candidate in CACHE_DIR.iterdir():
            if (
                candidate.is_file()
                and rotated_log_pattern.fullmatch(candidate.name)
            ):
                paths.append(candidate)
    except FileNotFoundError:
        pass
    except OSError:
        return False

    success = True
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            success = False
    return success

def format_command_for_display(cmd: list[str]) -> str:
    try:
        return shlex.join(cmd)
    except Exception:
        return " ".join(shlex.quote(str(part)) for part in cmd)


_REDACTED_VALUE = "<redacted>"
_SENSITIVE_OPTION_PARTS = (
    "access-key",
    "account-key",
    "api-key",
    "bearer",
    "connection-string",
    "credential",
    "key-pem",
    "otp",
    "pass",
    "password",
    "private-key",
    "sas-url",
    "secret",
    "token",
)
_URL_USERINFO_RE = re.compile(r"(?i)(https?://)[^/@\s]+@")
_COMMAND_LOG_MAX_BYTES = 1024 * 1024


def _sensitive_option_name(value: str) -> bool:
    name = value.strip().lstrip("-").replace("_", "-").casefold()
    return (
        name == "key"
        or name.endswith("-key")
        or any(part in name for part in _SENSITIVE_OPTION_PARTS)
    )


def _redact_command_parts(cmd: list[str]) -> list[str]:
    """Hide common rclone and shell secrets before persisting a command."""
    redacted: list[str] = []
    redact_next = False
    redact_shell_command = False
    shell = bool(cmd) and Path(str(cmd[0])).name in {
        "bash",
        "dash",
        "fish",
        "ksh",
        "sh",
        "zsh",
    }

    for raw_part in cmd:
        part = str(raw_part)
        if redact_next or redact_shell_command:
            redacted.append(_REDACTED_VALUE)
            redact_next = False
            redact_shell_command = False
            continue

        name, separator, _value = part.partition("=")
        if separator and _sensitive_option_name(name):
            redacted.append(f"{name}={_REDACTED_VALUE}")
            continue
        if not separator and part.startswith("-") and _sensitive_option_name(part):
            redacted.append(part)
            redact_next = True
            continue

        if "authorization:" in part.casefold():
            redacted.append(_REDACTED_VALUE)
            continue

        redacted.append(_URL_USERINFO_RE.sub(r"\1<redacted>@", part))
        if shell and part in {"-c", "-lc"}:
            redact_shell_command = True

    return redacted


def format_command_for_log(cmd: list[str]) -> str:
    return format_command_for_display(_redact_command_parts(cmd))


def write_command_log(profile_id: str, cmd: list[str]) -> None:
    ensure_runtime_dirs()
    path = command_file_for(profile_id)

    text = (
        f"[{datetime.now().isoformat(timespec='seconds')}]\n"
        f"{format_command_for_log(cmd)}\n\n"
    )

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.stat().st_size >= _COMMAND_LOG_MAX_BYTES:
            backup = path.with_name(f"{path.name}.1")
            try:
                backup.unlink()
            except FileNotFoundError:
                pass
            path.replace(backup)
            backup.chmod(0o600)
    except FileNotFoundError:
        pass
    except OSError:
        return

    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_APPEND
            | os.O_CREAT
            | os.O_CLOEXEC
            | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
        except OSError:
            os.close(descriptor)
            raise
        with os.fdopen(descriptor, "a", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


def human_size(value: int | float) -> str:
    value = float(value)
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]

    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024

    return f"{value:.1f} PiB"


def disk_usage_for_path(path: str) -> dict[str, Any] | None:
    path = expand_path(path)

    if not path:
        return None

    try:
        usage = shutil.disk_usage(path)
    except Exception:
        return None

    total = usage.total
    used = usage.used
    free = usage.free

    if total <= 0:
        fraction = 0.0
    else:
        fraction = min(max(used / total, 0.0), 1.0)

    return {
        "total": total,
        "used": used,
        "free": free,
        "fraction": fraction,
        "label": f"{human_size(used)} / {human_size(total)}",
        "used_label": human_size(used),
        "free_label": human_size(free),
        "total_label": human_size(total),
    }

def build_mount_command(profile_id: str, profile: dict[str, Any]) -> list[str]:
    ensure_runtime_dirs()

    remote_spec = str(profile.get("remote_spec", ""))
    mount_dir = expand_path(str(profile.get("mount_dir", "")))
    cache_dir = str(cache_dir_for(profile_id))
    log_file = str(log_file_for(profile_id))
    rc_socket = str(rc_socket_for(profile_id))

    Path(mount_dir).mkdir(parents=True, exist_ok=True)
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    values = {
        "remote_spec": remote_spec,
        "mount_dir": mount_dir,
        "cache_dir": cache_dir,
        "log_file": log_file,
        "rc_socket": rc_socket,
        "vfs_cache_mode": profile.get("vfs_cache_mode", "full"),
        "vfs_cache_max_size": profile.get("vfs_cache_max_size", "10G"),
        "vfs_cache_max_age": profile.get("vfs_cache_max_age", "24h"),
        "dir_cache_time": profile.get("dir_cache_time", "1h"),
        "umask": profile.get("umask", "022"),
    }

    custom = str(profile.get("custom_command", "")).strip()
    if custom:
        return _with_runtime_flags(shlex_format(custom, values), profile_id)

    return _with_runtime_flags([
        "rclone",
        "mount",
        remote_spec,
        mount_dir,
        "--vfs-cache-mode",
        str(values["vfs_cache_mode"]),
        "--vfs-cache-max-size",
        str(values["vfs_cache_max_size"]),
        "--vfs-cache-max-age",
        str(values["vfs_cache_max_age"]),
        "--dir-cache-time",
        str(values["dir_cache_time"]),
        "--umask",
        str(values["umask"]),
        "--cache-dir",
        cache_dir,
        "--log-file",
        log_file,
        "--log-level",
        "INFO",
    ], profile_id)


def _normalized_speed_limit(value: object) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in ("off", "0"):
        return "off"
    return text


def configured_bwlimit(settings: dict[str, Any] | None = None) -> str | None:
    settings = load_settings() if settings is None else settings
    upload = _normalized_speed_limit(settings.get("upload_speed_limit", ""))
    download = _normalized_speed_limit(settings.get("download_speed_limit", ""))
    if upload == "off" and download == "off":
        return None
    return f"{upload}:{download}"


def _with_runtime_flags(cmd: list[str], profile_id: str) -> list[str]:
    """Add monitoring and user-wide bandwidth flags to direct rclone mounts."""
    if len(cmd) < 2:
        return cmd
    if Path(cmd[0]).name != "rclone" or cmd[1] != "mount":
        return cmd

    # A custom command must not be able to move the unauthenticated RC API to
    # TCP. Remove every caller-provided listener and install one deterministic
    # Unix socket protected by runtime_dir() permissions instead.
    result: list[str] = []
    skip_rc_addr_value = False
    for part in cmd:
        if skip_rc_addr_value:
            skip_rc_addr_value = False
            continue
        if part == "--rc-addr":
            skip_rc_addr_value = True
            continue
        if part.startswith("--rc-addr="):
            continue
        if (
            part == "--rc"
            or part.startswith("--rc=")
            or part == "--rc-no-auth"
            or part.startswith("--rc-no-auth=")
        ):
            continue
        result.append(part)

    result.extend(
        [
            "--rc",
            "--rc-addr",
            f"unix://{rc_socket_for(profile_id)}",
            "--rc-no-auth",
        ]
    )

    bwlimit = configured_bwlimit()
    has_bwlimit = "--bwlimit" in result or any(
        part.startswith("--bwlimit=") for part in result
    )
    if bwlimit and not has_bwlimit:
        result.extend(["--bwlimit", bwlimit])

    rotation_options = (
        ("--log-file-max-size", "10M"),
        ("--log-file-max-backups", "3"),
        ("--log-file-max-age", "30d"),
    )
    for option, value in rotation_options:
        if option not in result and not any(
            part.startswith(f"{option}=") for part in result
        ):
            result.extend([option, value])
    return result


def apply_bwlimit_to_running_mounts(
    profiles: dict[str, dict[str, Any]],
    settings: dict[str, Any] | None = None,
) -> dict[str, bool]:
    rate = configured_bwlimit(settings) or "off"
    return {
        profile_id: set_bwlimit(profile_id, rate)
        for profile_id in profiles
        if rc_socket_for(profile_id).exists()
    }


def _mount_exec_environment() -> dict[str, str]:
    """Keep inherited settings except RC overrides that can add TCP listeners."""
    return {
        key: value
        for key, value in os.environ.items()
        if not (
            key.upper() == "RCLONE_RC"
            or key.upper().startswith("RCLONE_RC_")
        )
    }


def is_mounted_path(path: str) -> bool:
    path = expand_path(path)
    if command_exists("mountpoint"):
        return run_quiet(["mountpoint", "-q", path]) == 0
    return os.path.ismount(path)


def is_profile_mounted(profile: dict[str, Any]) -> bool:
    mount_dir = str(profile.get("mount_dir", ""))
    return bool(mount_dir) and is_mounted_path(mount_dir)


def mount_foreground(profile_id: str) -> None:
    profile = get_profile(profile_id)
    cmd = build_mount_command(profile_id, profile)
    write_command_log(profile_id, cmd)
    socket_path = rc_socket_for(profile_id)
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
    os.execvpe(cmd[0], cmd, _mount_exec_environment())


def unmount_raw(profile_id: str) -> bool:
    profile = get_profile(profile_id)
    mount_dir = expand_path(str(profile.get("mount_dir", "")))
    if not mount_dir:
        return False

    commands = [
        ["fusermount3", "-u", mount_dir],
        ["fusermount3", "-uz", mount_dir],
        ["fusermount", "-u", mount_dir],
        ["fusermount", "-uz", mount_dir],
        ["umount", mount_dir],
    ]
    for cmd in commands:
        if command_exists(cmd[0]) and run_quiet(cmd) == 0:
            return True
    return False


def open_mount_dir(profile_id: str) -> None:
    profile = get_profile(profile_id)
    mount_dir = expand_path(str(profile.get("mount_dir", "")))
    if mount_dir:
        Path(mount_dir).mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            ["xdg-open", mount_dir],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def show_file_in_nautilus(path: str) -> None:
    target = Path(expand_path(path))
    if target.is_file():
        nautilus_args = ["nautilus", "--select", str(target)]
        fallback_target = target.parent
    elif target.is_dir():
        nautilus_args = ["nautilus", str(target)]
        fallback_target = target
    elif target.parent.is_dir():
        nautilus_args = ["nautilus", str(target.parent)]
        fallback_target = target.parent
    else:
        return

    if command_exists("nautilus"):
        subprocess.Popen(
            nautilus_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return

    subprocess.Popen(
        ["xdg-open", str(fallback_target)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

def set_mount_folder_icon(profile: dict[str, Any]) -> bool:
    if not command_exists("gio"):
        return False

    mount_dir = expand_path(str(profile.get("mount_dir", "")))
    icon_name = str(profile.get("icon", DEFAULT_REMOTE_ICON_NAME))
    found_icon = remote_icon_path(icon_name)

    if not mount_dir or not found_icon:
        return False

    try:
        Path(mount_dir).mkdir(parents=True, exist_ok=True)
        icon_uri = Path(found_icon).resolve().as_uri()
    except Exception:
        return False

    return run_quiet(
        [
            "gio",
            "set",
            "-t",
            "string",
            mount_dir,
            "metadata::custom-icon",
            icon_uri,
        ]
    ) == 0


def clear_mount_folder_icon(profile: dict[str, Any]) -> bool:
    if not command_exists("gio"):
        return False

    mount_dir = expand_path(str(profile.get("mount_dir", "")))
    if not mount_dir:
        return False

    return run_quiet(
        [
            "gio",
            "set",
            "-d",
            mount_dir,
            "metadata::custom-icon",
        ]
    ) == 0
