from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import time
from typing import Any

from .activity_filter import DEFAULT_HIDDEN_ACTIVITY_PATTERNS, normalize_activity_patterns
from .paths import APP_DISPLAY_NAME, APP_ICON_NAME, APP_ID, AUTOSTART_FILE, SETTINGS_FILE, STATE_FILE, PROFILES_FILE, ensure_runtime_dirs
from .util import safe_id


DEFAULT_SETTINGS: dict[str, Any] = {
    "terminal_command": "gnome-terminal -- bash -lc {ssh_command}",
    "automount_previous": False,
    "auto_remount": False,
    "disable_tray_icon": False,
    "minimize_to_tray": False,
    "open_after_mount": False,
    "context_open_external_terminal": True,
    "context_copy_remote_path": True,
    "context_google_new_docs": True,
    "context_file_comparison": False,
    "upload_speed_limit": "",
    "download_speed_limit": "",
    "language": "system",
    "color_scheme": "system",
    "show_hidden_profiles": False,
    "activity_hidden_patterns": list(DEFAULT_HIDDEN_ACTIVITY_PATTERNS),
    "notify_transfer_complete": False,
    "notify_transfer_errors": True,
    "confirm_close_during_sync": False,
    "inhibit_shutdown_during_sync": False,
}


@contextmanager
def _file_lock(path: Path, *, exclusive: bool):
    """Serialize configuration access between GUI, tray, and monitor."""
    ensure_runtime_dirs()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_name(f".{path.name}.lock")
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC,
        0o600,
    )
    with os.fdopen(descriptor, "a+", encoding="utf-8") as stream:
        fcntl.flock(
            stream.fileno(),
            fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
        )
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _atomic_write_text(path: Path, text: str) -> None:
    """Write a private file without exposing a truncated intermediate state."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        path.chmod(0o600)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _backup_invalid_json(path: Path) -> None:
    if not path.exists():
        return
    try:
        json.loads(path.read_text(encoding="utf-8"))
        return
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    backup = path.with_name(
        f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    )
    try:
        shutil.copy2(path, backup)
        backup.chmod(0o600)
    except OSError:
        pass


def _valid_profile_id(profile_id: object) -> bool:
    value = str(profile_id)
    return bool(value) and safe_id(value) == value


def _load_profiles_data_unlocked() -> dict[str, Any]:
    if not PROFILES_FILE.exists():
        return {"profiles": {}}

    try:
        data = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"profiles": {}}

    if not isinstance(data, dict):
        data = {"profiles": {}}
    if not isinstance(data.get("profiles"), dict):
        data["profiles"] = {}
    else:
        data["profiles"] = {
            str(profile_id): profile
            for profile_id, profile in data["profiles"].items()
            if _valid_profile_id(profile_id) and isinstance(profile, dict)
        }
    return data


def load_profiles_data() -> dict[str, Any]:
    with _file_lock(PROFILES_FILE, exclusive=False):
        return _load_profiles_data_unlocked()


def save_profiles_data(data: dict[str, Any]) -> None:
    ensure_runtime_dirs()
    with _file_lock(PROFILES_FILE, exclusive=True):
        _backup_invalid_json(PROFILES_FILE)
        _atomic_write_text(
            PROFILES_FILE,
            json.dumps(data, indent=2, ensure_ascii=False),
        )


def list_profiles() -> dict[str, dict[str, Any]]:
    data = load_profiles_data()
    return data.get("profiles", {})


def get_profile(profile_id: str) -> dict[str, Any]:
    profiles = list_profiles()
    profile = profiles.get(profile_id)
    if not isinstance(profile, dict):
        raise KeyError(profile_id)
    return profile


def save_profile(profile_id: str, profile: dict[str, Any]) -> None:
    if not _valid_profile_id(profile_id):
        raise ValueError(f"invalid profile id: {profile_id!r}")
    ensure_runtime_dirs()
    with _file_lock(PROFILES_FILE, exclusive=True):
        _backup_invalid_json(PROFILES_FILE)
        data = _load_profiles_data_unlocked()
        data.setdefault("profiles", {})[profile_id] = profile
        _atomic_write_text(
            PROFILES_FILE,
            json.dumps(data, indent=2, ensure_ascii=False),
        )


def delete_profile(profile_id: str) -> None:
    delete_profiles([profile_id])


def delete_profiles(profile_ids: Iterable[str]) -> None:
    ids = {str(profile_id) for profile_id in profile_ids}
    if not ids:
        return

    ensure_runtime_dirs()
    with _file_lock(PROFILES_FILE, exclusive=True):
        _backup_invalid_json(PROFILES_FILE)
        data = _load_profiles_data_unlocked()
        profiles = data.setdefault("profiles", {})
        for profile_id in ids:
            profiles.pop(profile_id, None)
        _atomic_write_text(
            PROFILES_FILE,
            json.dumps(data, indent=2, ensure_ascii=False),
        )

    with _file_lock(STATE_FILE, exclusive=True):
        state = _load_json_file_unlocked(
            STATE_FILE,
            {"desired_mounted": [], "pending_unmount": []},
        )
        state["desired_mounted"] = sorted(
            str(profile_id)
            for profile_id in state.get("desired_mounted", [])
            if str(profile_id) not in ids
        )
        state["pending_unmount"] = sorted(
            str(profile_id)
            for profile_id in state.get("pending_unmount", [])
            if str(profile_id) not in ids
        )
        _atomic_write_text(
            STATE_FILE,
            json.dumps(state, indent=2, ensure_ascii=False),
        )


def profiles_for_remote(
    remote_name: str,
    profiles: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    expected = remote_name.strip().rstrip(":").casefold()
    source = list_profiles() if profiles is None else profiles
    return {
        profile_id: profile
        for profile_id, profile in source.items()
        if str(profile.get("remote_name", "")).strip().rstrip(":").casefold()
        == expected
    }


def _load_json_file_unlocked(
    path: Path,
    fallback: dict[str, Any],
) -> dict[str, Any]:
    if not path.exists():
        return dict(fallback)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return dict(fallback)

    if not isinstance(data, dict):
        return dict(fallback)

    return {**fallback, **data}


def _load_json_file(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    ensure_runtime_dirs()
    with _file_lock(path, exclusive=False):
        return _load_json_file_unlocked(path, fallback)


def _save_json_file(path: Path, data: dict[str, Any]) -> None:
    ensure_runtime_dirs()
    with _file_lock(path, exclusive=True):
        _backup_invalid_json(path)
        _atomic_write_text(
            path,
            json.dumps(data, indent=2, ensure_ascii=False),
        )


def load_settings() -> dict[str, Any]:
    data = _load_json_file(SETTINGS_FILE, DEFAULT_SETTINGS)
    data["activity_hidden_patterns"] = normalize_activity_patterns(
        data.get("activity_hidden_patterns")
    )
    return data


def save_settings(settings: dict[str, Any]) -> None:
    data = {**DEFAULT_SETTINGS, **settings}
    data["activity_hidden_patterns"] = normalize_activity_patterns(
        data.get("activity_hidden_patterns")
    )
    _save_json_file(SETTINGS_FILE, data)
    set_tray_autostart_enabled(bool(data.get("automount_previous")))


def load_state() -> dict[str, Any]:
    data = _load_json_file(
        STATE_FILE,
        {"desired_mounted": [], "pending_unmount": []},
    )
    if not isinstance(data.get("desired_mounted"), list):
        data["desired_mounted"] = []
    if not isinstance(data.get("pending_unmount"), list):
        data["pending_unmount"] = []
    return data


def update_sponsor_reminder_state(
    *,
    first_used_at: float | None = None,
    remind_after: float | None = None,
    disabled: bool | None = None,
) -> None:
    """Update sponsor reminder fields without discarding mount state."""
    ensure_runtime_dirs()
    with _file_lock(STATE_FILE, exclusive=True):
        state = _load_json_file_unlocked(
            STATE_FILE,
            {"desired_mounted": [], "pending_unmount": []},
        )
        if first_used_at is not None:
            state["sponsor_first_used_at"] = float(first_used_at)
        if remind_after is not None:
            state["sponsor_remind_after"] = float(remind_after)
        if disabled is not None:
            state["sponsor_reminder_disabled"] = bool(disabled)
        _atomic_write_text(
            STATE_FILE,
            json.dumps(state, indent=2, ensure_ascii=False),
        )


def desired_mounted_profiles() -> set[str]:
    return {str(item) for item in load_state().get("desired_mounted", [])}


def set_profile_desired_mounted(profile_id: str, desired: bool) -> None:
    ensure_runtime_dirs()
    with _file_lock(STATE_FILE, exclusive=True):
        state = _load_json_file_unlocked(
            STATE_FILE,
            {"desired_mounted": [], "pending_unmount": []},
        )
        profiles = {
            str(item) for item in state.get("desired_mounted", [])
        }
        if desired:
            profiles.add(profile_id)
        else:
            profiles.discard(profile_id)
        state["desired_mounted"] = sorted(profiles)
        _atomic_write_text(
            STATE_FILE,
            json.dumps(state, indent=2, ensure_ascii=False),
        )


def pending_unmount_profiles() -> set[str]:
    return {str(item) for item in load_state().get("pending_unmount", [])}


def set_profile_pending_unmount(profile_id: str, pending: bool) -> None:
    ensure_runtime_dirs()
    with _file_lock(STATE_FILE, exclusive=True):
        state = _load_json_file_unlocked(
            STATE_FILE,
            {"desired_mounted": [], "pending_unmount": []},
        )
        profiles = {
            str(item) for item in state.get("pending_unmount", [])
        }
        if pending:
            profiles.add(profile_id)
        else:
            profiles.discard(profile_id)
        state["pending_unmount"] = sorted(profiles)
        _atomic_write_text(
            STATE_FILE,
            json.dumps(state, indent=2, ensure_ascii=False),
        )


def _installed_executable() -> str:
    found = shutil.which(APP)
    if found:
        return found
    return sys.argv[0] or APP


def set_tray_autostart_enabled(enabled: bool) -> None:
    AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not enabled:
        if AUTOSTART_FILE.exists():
            AUTOSTART_FILE.unlink()
        return

    exec_path = _installed_executable()
    command = f"{shlex.quote(exec_path)} tray"
    _atomic_write_text(
        AUTOSTART_FILE,
        "\n".join(
            [
                "[Desktop Entry]",
                "Type=Application",
                f"Name={APP_DISPLAY_NAME}",
                f"Exec={command}",
                f"Icon={APP_ICON_NAME}",
                "Terminal=false",
                "X-GNOME-Autostart-enabled=true",
                f"StartupWMClass={APP_ID}",
                "",
            ]
        ),
    )
