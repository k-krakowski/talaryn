from __future__ import annotations

import fcntl
import os
from pathlib import Path
import shutil
import stat
import subprocess

APP = "talaryn"
APP_DISPLAY_NAME = "Talaryn"
APP_ID = "io.github.k_krakowski.Talaryn"
COMPARISON_APP_ID = f"{APP_ID}.Comparison"
GOOGLE_CREATE_APP_ID = f"{APP_ID}.GoogleCreate"
APP_ICON_NAME = "talaryn"
APP_ICON_FILE = "talaryn-app-icon.svg"
APP_INDICATOR_ICON_NAME = Path(APP_ICON_FILE).stem
LEGACY_APP = "rclone-mount-gui"
LEGACY_APP_ID = "io.github.rclonemountgui.RcloneMountGui"
LEGACY_APP_ICON_NAME = "rclone-mount-gui-icon"

CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
CACHE_HOME = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
CONFIG_DIR = CONFIG_HOME / APP
CACHE_DIR = CACHE_HOME / APP
LEGACY_CONFIG_DIR = CONFIG_HOME / LEGACY_APP
LEGACY_CACHE_DIR = CACHE_HOME / LEGACY_APP
RUNTIME_DIR = Path(
    os.environ.get("XDG_RUNTIME_DIR", str(CACHE_DIR / "runtime"))
) / APP
FALLBACK_RUNTIME_DIR = CACHE_DIR / "runtime" / APP
DATA_HOME = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
PROFILES_FILE = CONFIG_DIR / "profiles.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
STATE_FILE = CONFIG_DIR / "state.json"
ACTIVITY_DB = CACHE_DIR / "activity.db"
SYNC_SNAPSHOT_FILE = CACHE_DIR / "sync-status.json"
NAUTILUS_STATUS_FILE = CACHE_DIR / "nautilus-status.json"
COMPARISON_SELECTION_FILE = CACHE_DIR / "file-comparison.json"
AUTOSTART_DIR = CONFIG_DIR.parent / "autostart"
AUTOSTART_FILE = AUTOSTART_DIR / f"{APP_ID}.tray.desktop"
LEGACY_AUTOSTART_FILE = AUTOSTART_DIR / f"{LEGACY_APP_ID}.tray.desktop"
LEGACY_RUNTIME_DIR = Path(
    os.environ.get(
        "XDG_RUNTIME_DIR",
        str(LEGACY_CACHE_DIR / "runtime"),
    )
) / LEGACY_APP
LEGACY_FALLBACK_RUNTIME_DIR = LEGACY_CACHE_DIR / "runtime" / LEGACY_APP

# Project root in a checkout: <root>/src/talaryn/paths.py -> <root>
PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ACTIVE_RUNTIME_DIR: Path | None = None


def _ensure_private_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        try:
            os.fchmod(descriptor, 0o700)
            metadata = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise RuntimeError(f"cannot secure private directory: {path}") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"private path is not a directory: {path}")
    if metadata.st_uid != os.geteuid():
        raise RuntimeError(f"private directory has a different owner: {path}")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise RuntimeError(f"private directory has unsafe permissions: {path}")
    return path


def _owned_directory(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.geteuid()


def _move_legacy_directory(source: Path, destination: Path) -> bool:
    """Atomically adopt an old private directory without following symlinks."""
    if destination.exists() or not _owned_directory(source):
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        source.replace(destination)
        _ensure_private_directory(destination)
    except OSError:
        return False
    return True


def _copy_legacy_custom_icons(source: Path, destination: Path) -> None:
    if destination.exists() or not _owned_directory(source):
        return
    destination.mkdir(parents=True, mode=0o700)
    for icon in source.iterdir():
        try:
            metadata = icon.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            continue
        try:
            shutil.copy2(icon, destination / icon.name, follow_symlinks=False)
        except OSError:
            continue
    _ensure_private_directory(destination)


def _migrate_autostart_file(source: Path, destination: Path) -> None:
    try:
        metadata = source.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            return
        if destination.exists():
            source.unlink()
            return
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return
    updated = (
        text.replace(f"Name={LEGACY_APP}", f"Name={APP_DISPLAY_NAME}")
        .replace(LEGACY_APP_ID, APP_ID)
        .replace(LEGACY_APP_ICON_NAME, APP_ICON_NAME)
        .replace(LEGACY_APP, APP)
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(updated)
        source.unlink()
    except OSError:
        return


def _legacy_lock_is_held(path: Path) -> bool:
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(descriptor)


def _legacy_mount_units_active() -> bool:
    try:
        result = subprocess.run(
            [
                "systemctl",
                "--user",
                "list-units",
                "--type=service",
                "--state=active,activating",
                "--no-legend",
                "--plain",
                f"{LEGACY_APP}@*.service",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return bool(result.stdout.strip())


def _stop_legacy_monitor() -> None:
    try:
        subprocess.run(
            [
                "systemctl",
                "--user",
                "disable",
                "--now",
                f"{LEGACY_APP}-monitor.service",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _legacy_migration_needed() -> bool:
    return any(
        (
            LEGACY_CONFIG_DIR.exists() and not CONFIG_DIR.exists(),
            LEGACY_CACHE_DIR.exists() and not CACHE_DIR.exists(),
            LEGACY_AUTOSTART_FILE.exists(),
            (DATA_HOME / LEGACY_APP / "custom-icons").exists()
            and not (DATA_HOME / APP / "custom-icons").exists(),
        )
    )


def prepare_legacy_migration() -> None:
    """Quiesce the former installation before adopting writable state."""
    if os.environ.get("TALARYN_DISABLE_LEGACY_MIGRATION") == "1":
        return
    if not _legacy_migration_needed():
        return
    _stop_legacy_monitor()
    if _legacy_mount_units_active():
        raise RuntimeError(
            "Unmount all profiles from the previous rclone-mount-gui "
            "installation before starting Talaryn."
        )
    if any(
        _legacy_lock_is_held(directory / "tray.lock")
        for directory in (LEGACY_RUNTIME_DIR, LEGACY_FALLBACK_RUNTIME_DIR)
    ):
        raise RuntimeError(
            "Close the previous rclone-mount-gui panel icon before "
            "starting Talaryn."
        )


def migrate_legacy_user_data(
    legacy_config_dir: Path = LEGACY_CONFIG_DIR,
    config_dir: Path = CONFIG_DIR,
    legacy_cache_dir: Path = LEGACY_CACHE_DIR,
    cache_dir: Path = CACHE_DIR,
    legacy_data_dir: Path = DATA_HOME / LEGACY_APP,
    data_dir: Path = DATA_HOME / APP,
    legacy_autostart_file: Path = LEGACY_AUTOSTART_FILE,
    autostart_file: Path = AUTOSTART_FILE,
) -> None:
    """Move pre-Talaryn state once while preserving user-created icons."""
    _move_legacy_directory(legacy_config_dir, config_dir)
    cache_moved = _move_legacy_directory(legacy_cache_dir, cache_dir)
    if cache_moved:
        for filename in (
            "sync-status.json",
            "nautilus-status.json",
            "monitor-error.log",
        ):
            try:
                (cache_dir / filename).unlink()
            except FileNotFoundError:
                pass
    _copy_legacy_custom_icons(
        legacy_data_dir / "custom-icons",
        data_dir / "custom-icons",
    )
    _migrate_autostart_file(legacy_autostart_file, autostart_file)


def data_dirs() -> list[Path]:
    dirs: list[Path] = []

    env_dir = os.environ.get("TALARYN_DATA_DIR") or os.environ.get(
        "RCLONE_MOUNT_GUI_DATA_DIR"
    )
    if env_dir:
        dirs.append(Path(env_dir))

    # Development checkout layout.
    dirs.append(PROJECT_ROOT / "data")
    dirs.append(PROJECT_ROOT)

    # User and system install layouts.
    dirs.append(DATA_HOME / APP)
    dirs.append(Path("/usr/share") / APP)

    # Deduplicate while preserving order.
    unique: list[Path] = []
    seen: set[str] = set()
    for item in dirs:
        key = str(item)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def ensure_runtime_dirs() -> None:
    prepare_legacy_migration()
    migrate_legacy_user_data()
    for directory in (CONFIG_DIR, CACHE_DIR):
        _ensure_private_directory(directory)
    runtime_dir()
    (Path.home() / "cloud").mkdir(parents=True, exist_ok=True)


def runtime_dir() -> Path:
    global _ACTIVE_RUNTIME_DIR
    if _ACTIVE_RUNTIME_DIR is not None:
        return _ACTIVE_RUNTIME_DIR
    selected = RUNTIME_DIR
    try:
        _ensure_private_directory(selected)
    except (OSError, RuntimeError):
        selected = FALLBACK_RUNTIME_DIR
        _ensure_private_directory(selected)
    _ACTIVE_RUNTIME_DIR = selected
    return selected


def monitor_lock_file() -> Path:
    return runtime_dir() / "monitor.lock"
