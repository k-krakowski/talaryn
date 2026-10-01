from __future__ import annotations

import fcntl
import os
import subprocess
import time
from typing import Any

from gi.repository import Gio, GLib

from .activity_store import ActivityStore
from .config import (
    list_profiles,
    load_settings,
    pending_unmount_profiles,
    set_profile_pending_unmount,
)
from .i18n import tr
from .mount import (
    apply_bwlimit_to_running_mounts,
    clear_mount_folder_icon,
    human_size,
    unmount_raw,
)
from .paths import (
    CACHE_DIR,
    SETTINGS_FILE,
    ensure_runtime_dirs,
    monitor_lock_file,
)
from .sync_status import (
    activity_group_key,
    collect_sync_activity,
    read_nautilus_status_snapshot,
    read_sync_snapshot,
    write_sync_snapshot,
)
from .systemd import stop as systemd_stop
from .util import application_command, notify
from .transfer_safety import probe_transfer_state, snapshot_allows_unmount


ACTIVE_REFRESH_SECONDS = 1.0
IDLE_REFRESH_SECONDS = 4.0
INHIBITOR_RETRY_SECONDS = 30.0
NOTIFICATION_GROUP_SETTLE_SECONDS = 5.0
NOTIFICATION_GROUP_RETENTION_SECONDS = 3600.0
LEGACY_MONITOR_SERVICE = "rclone-mount-gui-monitor.service"


def _stop_legacy_monitor_service() -> None:
    try:
        subprocess.run(
            [
                "systemctl",
                "--user",
                "disable",
                "--now",
                LEGACY_MONITOR_SERVICE,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _open_shutdown_inhibitor() -> int:
    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    reply, fd_list = bus.call_with_unix_fd_list_sync(
        "org.freedesktop.login1",
        "/org/freedesktop/login1",
        "org.freedesktop.login1.Manager",
        "Inhibit",
        GLib.Variant(
            "(ssss)",
            (
                "shutdown",
                tr("app_title"),
                tr("shutdown_inhibit_reason"),
                "block",
            ),
        ),
        GLib.VariantType.new("(h)"),
        Gio.DBusCallFlags.NONE,
        3000,
        None,
        None,
    )
    if fd_list is None:
        raise RuntimeError("logind did not return an inhibitor descriptor")
    handle = int(reply.unpack()[0])
    descriptor = fd_list.get(handle)
    if descriptor < 0:
        raise RuntimeError("logind returned an invalid inhibitor descriptor")
    return descriptor


class ShutdownInhibitor:
    def __init__(self) -> None:
        self._descriptor: int | None = None
        self._retry_after = 0.0

    @property
    def active(self) -> bool:
        return self._descriptor is not None

    def update(self, enabled: bool, syncing: bool) -> None:
        if not enabled or not syncing:
            self.release()
            return
        if self._descriptor is not None:
            return
        now = time.monotonic()
        if now < self._retry_after:
            return
        try:
            self._descriptor = _open_shutdown_inhibitor()
            self._retry_after = 0.0
        except Exception as error:
            self._retry_after = now + INHIBITOR_RETRY_SECONDS
            _write_monitor_error(
                RuntimeError(f"failed to inhibit system shutdown: {error}")
            )

    def release(self) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        self._retry_after = 0.0
        if descriptor is None:
            return
        try:
            os.close(descriptor)
        except OSError:
            pass


def ensure_monitor_running() -> None:
    full_snapshot = read_sync_snapshot()
    nautilus_snapshot = read_nautilus_status_snapshot()
    if full_snapshot is not None and nautilus_snapshot is not None:
        return
    migration_restart = (
        full_snapshot is not None and nautilus_snapshot is None
    )
    actions = (
        (
            ["daemon-reload"],
            ["restart", "talaryn-monitor.service"],
            ["enable", "--now", "talaryn-monitor.service"],
        )
        if migration_restart
        else (
            ["enable", "--now", "talaryn-monitor.service"],
            ["daemon-reload"],
            ["enable", "--now", "talaryn-monitor.service"],
        )
    )
    try:
        for action in actions:
            service = subprocess.run(
                ["systemctl", "--user", *action],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=2,
            )
            if action[0] == "enable" and service.returncode == 0:
                return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        subprocess.Popen(
            application_command("monitor"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


def _settings_stamp() -> tuple[int, int] | None:
    try:
        stat = SETTINGS_FILE.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _format_transfer_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 1:
        return "<1 s"
    if seconds < 60:
        return f"{seconds} s"
    minutes, remaining_seconds = divmod(seconds, 60)
    if minutes < 60:
        return (
            f"{minutes} min {remaining_seconds} s"
            if remaining_seconds
            else f"{minutes} min"
        )
    hours, remaining_minutes = divmod(minutes, 60)
    if hours < 24:
        return (
            f"{hours} h {remaining_minutes} min"
            if remaining_minutes
            else f"{hours} h"
        )
    days, remaining_hours = divmod(hours, 24)
    return (
        f"{days} d {remaining_hours} h"
        if remaining_hours
        else f"{days} d"
    )


def _event_group_timestamp(item: dict[str, Any]) -> float:
    for field in ("group_timestamp", "first_seen", "timestamp"):
        try:
            value = float(item.get(field, 0) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0.0


class TransferNotificationBatcher:
    """Emit at most one completion notification per activity group."""

    def __init__(self) -> None:
        self._groups: dict[str, dict[str, Any]] = {}
        self._notified: dict[str, float] = {}

    def update(
        self,
        activity: dict[str, Any],
        settings: dict[str, Any],
        *,
        now: float | None = None,
        ingest_events: bool = True,
    ) -> None:
        now = time.time() if now is None else float(now)
        live_items = [
            item
            for name in ("active", "queued")
            for item in activity.get(name, [])
            if isinstance(item, dict)
        ]
        live_keys = {activity_group_key(item) for item in live_items}
        for key in live_keys:
            group = self._groups.get(key)
            if group is not None:
                group["last_change"] = now

        if ingest_events:
            for item in activity.get("new_events", []):
                if not isinstance(item, dict):
                    continue
                if item.get("state") != "completed":
                    continue
                if item.get("operation") not in ("copy", "modify"):
                    continue
                key = activity_group_key(item)
                if key in self._notified:
                    continue
                started_at = _event_group_timestamp(item)
                try:
                    finished_at = float(item.get("timestamp", 0) or 0)
                except (TypeError, ValueError):
                    finished_at = 0.0
                group = self._groups.setdefault(
                    key,
                    {
                        "files": {},
                        "started_at": started_at,
                        "finished_at": finished_at,
                        "last_change": now,
                    },
                )
                file_key = str(item.get("path", "")).strip().strip("/")
                if not file_key:
                    file_key = str(item.get("event_id", ""))
                group["files"][file_key] = item
                if started_at > 0:
                    current_start = float(group.get("started_at", 0) or 0)
                    group["started_at"] = (
                        min(current_start, started_at)
                        if current_start > 0
                        else started_at
                    )
                group["finished_at"] = max(
                    float(group.get("finished_at", 0) or 0),
                    finished_at,
                )
                group["last_change"] = now

        ready = [
            key
            for key, group in self._groups.items()
            if key not in live_keys
            and now - float(group.get("last_change", now))
            >= NOTIFICATION_GROUP_SETTLE_SECONDS
        ]
        for key in ready:
            group = self._groups.pop(key)
            self._notified[key] = now
            if settings.get("notify_transfer_complete", False):
                self._notify_group(group)

        self._notified = {
            key: notified_at
            for key, notified_at in self._notified.items()
            if now - notified_at <= NOTIFICATION_GROUP_RETENTION_SECONDS
        }

    def _notify_group(self, group: dict[str, Any]) -> None:
        files = list(group.get("files", {}).values())
        if not files:
            return
        total_size = 0
        for item in files:
            try:
                total_size += max(0, int(item.get("size", 0) or 0))
            except (TypeError, ValueError):
                continue
        started_at = float(group.get("started_at", 0) or 0)
        finished_at = float(group.get("finished_at", 0) or 0)
        duration = max(0.0, finished_at - started_at)
        notify(
            tr("notification_transfer_complete"),
            tr(
                (
                    "notification_transfer_single_summary"
                    if len(files) == 1
                    else "notification_transfer_group_summary"
                ),
                count=len(files),
                size=human_size(total_size),
                duration=_format_transfer_duration(duration),
            ),
        )


def _notify_transfer_errors(
    events: list[dict[str, Any]],
    settings: dict[str, Any],
) -> None:
    failed = [item for item in events if item.get("state") == "error"]

    if failed and settings.get("notify_transfer_errors", True):
        first = failed[0]
        detail = str(first.get("error") or first.get("name", ""))
        notify(
            tr("notification_transfer_failed"),
            detail if len(failed) == 1 else f"{detail}\n+{len(failed) - 1}",
        )


def _notify_deleted_google_links(
    events: list[dict[str, Any]],
    profiles: dict[str, dict[str, Any]],
) -> None:
    deleted: dict[str, dict[str, Any]] = {}
    for item in events:
        if item.get("state") != "completed" or item.get("operation") != "delete":
            continue
        path = str(item.get("path", "")).strip()
        if not path.casefold().endswith(".link.html"):
            continue
        profile = profiles.get(str(item.get("profile_id", "")), {})
        if str(profile.get("kind", "")) != "gdrive":
            continue
        key = str(item.get("event_id", "")) or path
        deleted[key] = item

    if not deleted:
        return
    items = list(deleted.values())
    if len(items) == 1:
        item = items[0]
        name = str(item.get("name") or Path(str(item["path"])).name)
        notify(
            tr("google_link_deleted_title"),
            tr("google_link_deleted_single", name=name),
        )
        return
    notify(
        tr("google_link_deleted_title"),
        tr("google_link_deleted_group", count=len(items)),
    )


def _write_monitor_error(error: Exception) -> None:
    try:
        (CACHE_DIR / "monitor-error.log").write_text(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} {error}\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def finish_pending_unmounts(
    activity: dict[str, Any], profiles: dict[str, dict[str, Any]],
) -> None:
    for profile_id in pending_unmount_profiles():
        profile = profiles.get(profile_id)
        if profile is None:
            set_profile_pending_unmount(profile_id, False)
            continue
        if not snapshot_allows_unmount(activity, profile_id):
            continue
        if probe_transfer_state(profile_id) != "idle":
            continue
        stopped = systemd_stop(profile_id)
        if not stopped:
            stopped = unmount_raw(profile_id)
        if stopped:
            clear_mount_folder_icon(profile)
            set_profile_pending_unmount(profile_id, False)


def run_monitor() -> int:
    os.umask(0o077)
    _stop_legacy_monitor_service()
    ensure_runtime_dirs()
    try:
        descriptor = os.open(
            monitor_lock_file(),
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
    except OSError:
        return 1
    try:
        os.fchmod(descriptor, 0o600)
    except OSError:
        os.close(descriptor)
        return 1
    lock_stream = os.fdopen(descriptor, "a+", encoding="utf-8")
    try:
        fcntl.flock(lock_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_stream.close()
        return 0
    except OSError:
        lock_stream.close()
        return 1

    store = ActivityStore()
    shutdown_inhibitor = ShutdownInhibitor()
    notification_batcher = TransferNotificationBatcher()
    initialized = False
    settings_stamp: tuple[int, int] | None = None
    try:
        while True:
            try:
                profiles = list_profiles()
                settings = load_settings()
                inhibit_shutdown = bool(
                    settings.get("inhibit_shutdown_during_sync", False)
                )
                if not inhibit_shutdown:
                    shutdown_inhibitor.release()
                current_stamp = _settings_stamp()
                if current_stamp != settings_stamp:
                    apply_bwlimit_to_running_mounts(profiles, settings)
                    settings_stamp = current_stamp

                activity = collect_sync_activity(profiles, store=store)
                shutdown_inhibitor.update(
                    inhibit_shutdown,
                    bool(activity.get("is_syncing")),
                )
                write_sync_snapshot(activity)
                finish_pending_unmounts(activity, profiles)
                notification_batcher.update(
                    activity,
                    settings,
                    ingest_events=initialized,
                )
                if initialized:
                    _notify_transfer_errors(
                        activity.get("new_events", []),
                        settings,
                    )
                    _notify_deleted_google_links(
                        activity.get("new_events", []),
                        profiles,
                    )
                initialized = True
                delay = (
                    ACTIVE_REFRESH_SECONDS
                    if activity.get("is_syncing")
                    or float(activity.get("total_speed", 0) or 0) > 0
                    else IDLE_REFRESH_SECONDS
                )
            except KeyboardInterrupt:
                break
            except Exception as error:
                _write_monitor_error(error)
                delay = IDLE_REFRESH_SECONDS
            time.sleep(delay)
    finally:
        shutdown_inhibitor.release()
        lock_stream.close()
    return 0
