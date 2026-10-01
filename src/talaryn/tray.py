from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import (
    desired_mounted_profiles,
    list_profiles,
    load_settings,
    set_profile_desired_mounted,
    set_profile_pending_unmount,
)
from .icons import icon_path
from .i18n import tr
from .mount import (
    clear_mount_folder_icon,
    is_profile_mounted,
    log_file_for,
    open_mount_dir,
    unmount_raw,
    human_size,
)
from .monitor import ensure_monitor_running
from .paths import (
    APP_ICON_FILE,
    APP_INDICATOR_ICON_NAME,
    APP_ID,
    CACHE_DIR,
    ensure_runtime_dirs,
    runtime_dir,
)
from .systemd import start as systemd_start
from .systemd import is_active as systemd_is_active
from .systemd import stop as systemd_stop
from .sync_status import get_sync_activity
from .util import application_command, notify
from .transfer_safety import probe_transfer_state, snapshot_allows_unmount

import gi

gi.require_version("Gtk", "3.0")

try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3  # type: ignore
except (ValueError, ImportError):
    try:
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3  # type: ignore
    except (ValueError, ImportError):
        AppIndicator3 = None  # type: ignore

from gi.repository import Gio, GLib, Gtk  # noqa: E402


def _tray_log(message: str) -> None:
    path = CACHE_DIR / "tray.log"
    try:
        ensure_runtime_dirs()
        if path.exists() and path.stat().st_size >= 1024 * 1024:
            backup = path.with_name("tray.log.1")
            try:
                backup.unlink()
            except FileNotFoundError:
                pass
            path.replace(backup)
            backup.chmod(0o600)
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
            f.write(f"{message}\n")
    except OSError:
        pass


class TrayApp:
    def __init__(self) -> None:
        if AppIndicator3 is None:
            raise RuntimeError("Missing AppIndicator3 typelib")

        settings = Gtk.Settings.get_default()
        if settings is not None:
            settings.set_property("gtk-menu-images", True)

        self.indicator = AppIndicator3.Indicator.new(
            APP_ID,
            APP_INDICATOR_ICON_NAME,
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        app_icon_path = icon_path(APP_ICON_FILE)
        if app_icon_path and hasattr(self.indicator, "set_icon_theme_path"):
            self.indicator.set_icon_theme_path(str(Path(app_icon_path).parent))
        if hasattr(self.indicator, "set_icon_full"):
            self.indicator.set_icon_full(
                APP_INDICATOR_ICON_NAME,
                tr("app_title"),
            )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.menu = Gtk.Menu()
        self.menu.connect("deactivate", self._on_menu_deactivated)
        self.indicator.set_menu(self.menu)
        self.sync_status_item: Gtk.MenuItem | None = None
        self._menu_state: tuple[bool, tuple[tuple[str, str, bool], ...]] = (False, ())
        self._menu_refresh_pending = False
        self._auto_remounting: set[str] = set()
        self._pending_unmount: set[str] = set()
        self._cleared_unmounted_icons: set[str] = set()
        self._volume_monitor = Gio.VolumeMonitor.get()
        self._volume_monitor.connect(
            "mount-added", self._on_system_mount_changed
        )
        self._volume_monitor.connect(
            "mount-removed", self._on_system_mount_changed
        )
        self._volume_monitor.connect(
            "mount-changed", self._on_system_mount_changed
        )
        ensure_monitor_running()
        if load_settings().get("automount_previous"):
            self._mount_desired_profiles()
        self.refresh_menu()
        GLib.timeout_add_seconds(5, self._periodic_refresh)

    def _periodic_refresh(self) -> bool:
        if load_settings().get("disable_tray_icon"):
            Gtk.main_quit()
            return False
        settings = load_settings()
        if settings.get("auto_remount"):
            self._remount_missing_desired_profiles()
        self._refresh_menu_if_state_changed()
        self._refresh_sync_status()
        self._finish_pending_unmounts()
        return True

    @staticmethod
    def _ordered_profiles(
        profiles: dict[str, dict[str, Any]],
    ) -> list[tuple[str, dict[str, Any]]]:
        return sorted(
            profiles.items(),
            key=lambda item: str(
                item[1].get("label", item[0])
            ).lower(),
        )

    def _current_menu_state(
        self,
        profiles: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[bool, tuple[tuple[str, str, bool], ...]]:
        profiles = list_profiles() if profiles is None else profiles
        profile_state = tuple(
            (
                profile_id,
                str(profile.get("label", profile_id)),
                is_profile_mounted(profile),
            )
            for profile_id, profile in self._ordered_profiles(profiles)
        )
        return bool(load_settings().get("context_file_comparison", False)), profile_state

    def _refresh_menu_if_state_changed(self) -> bool:
        state = self._current_menu_state()
        if state == self._menu_state:
            return False
        if self.menu.get_mapped():
            self._menu_refresh_pending = True
            return False
        self.refresh_menu()
        return True

    def _on_system_mount_changed(self, *_args: object) -> None:
        self._refresh_menu_if_state_changed()

    def _on_menu_deactivated(self, _menu: Gtk.Menu) -> None:
        if not self._menu_refresh_pending:
            return
        self._menu_refresh_pending = False
        GLib.idle_add(self._refresh_once)

    def refresh_menu(self) -> None:
        for child in self.menu.get_children():
            self.menu.remove(child)

        profiles = list_profiles()
        mounted_ids: list[str] = []
        unmounted_ids: list[str] = []

        menu_state: list[tuple[str, str, bool]] = []
        for profile_id, profile in self._ordered_profiles(profiles):
            mounted = is_profile_mounted(profile)
            menu_state.append(
                (
                    profile_id,
                    str(profile.get("label", profile_id)),
                    mounted,
                )
            )
            if mounted:
                self._cleared_unmounted_icons.discard(profile_id)
                mounted_ids.append(profile_id)
            else:
                if profile_id not in self._cleared_unmounted_icons:
                    clear_mount_folder_icon(profile)
                    self._cleared_unmounted_icons.add(profile_id)
                unmounted_ids.append(profile_id)

        self._append_heading(
            tr(
                "tray_status",
                mounted=len(mounted_ids),
                total=len(profiles),
            )
        )
        activity = get_sync_activity()
        self.sync_status_item = self._append_disabled(
            self._sync_status_label(activity)
        )
        self._append_separator()

        if not profiles:
            self._append_disabled(tr("no_profiles"))
        else:
            for profile_id in mounted_ids + unmounted_ids:
                profile = profiles[profile_id]
                self._append_profile(
                    profile_id,
                    profile,
                    profile_id in mounted_ids,
                    activity.get("profiles", {}).get(profile_id, {}),
                )

        self._append_separator()
        self._append_item(tr("open_app"), self._open_app)
        comparison_enabled = bool(load_settings().get("context_file_comparison", False))
        if comparison_enabled:
            self._append_item(
                tr("show_file_comparison"),
                self._open_comparison,
            )
        self._append_item(tr("quit_tray"), self._quit_application)
        self._menu_state = comparison_enabled, tuple(menu_state)
        self._menu_refresh_pending = False
        self.menu.show_all()

    def _append_profile(
        self,
        profile_id: str,
        profile: dict[str, Any],
        mounted: bool,
        sync_state: dict[str, Any],
    ) -> None:
        label = str(profile.get("label", profile_id))
        state = tr("status_mounted") if mounted else tr("status_unmounted")
        if mounted and sync_state.get("is_syncing"):
            state = tr(
                "tray_profile_syncing",
                active=int(sync_state.get("active_count", 0)),
                queued=int(sync_state.get("queued_count", 0)),
            )
        elif mounted and (
            sync_state.get("cache_out_of_space")
            or sync_state.get("cache_error_files")
            or sync_state.get("fatal_error")
        ):
            state = tr("tray_profile_error")

        root = Gtk.MenuItem(label=f"{label} - {state}")
        submenu = Gtk.Menu()
        root.set_submenu(submenu)

        if mounted:
            self._append_icon_item(
                submenu,
                tr("open_in_nautilus"),
                "folder-open-symbolic",
                self._open_profile,
                profile_id,
            )
            self._append_icon_item(
                submenu,
                tr("unmount"),
                "media-eject-symbolic",
                self._unmount_profile,
                profile_id,
            )
        else:
            self._append_icon_item(
                submenu,
                tr("mount"),
                "drive-harddisk-symbolic",
                self._mount_profile,
                profile_id,
            )

        self._append_icon_item(
            submenu,
            tr("show_log"),
            "text-x-generic-symbolic",
            self._show_log,
            profile_id,
        )

        self.menu.append(root)

    def _append_heading(self, label: str) -> None:
        self._append_disabled(label)

    def _append_disabled(self, label: str) -> Gtk.MenuItem:
        item = Gtk.MenuItem(label=label)
        item.set_sensitive(False)
        self.menu.append(item)
        return item

    def _sync_status_label(self, activity: dict[str, Any]) -> str:
        if activity["is_syncing"]:
            return tr(
                "tray_syncing",
                active=activity["total_active_count"],
                queued=activity["queued_count"],
                speed=f"{human_size(float(activity.get('total_speed', 0) or 0))}/s",
            )
        if int(activity.get("error_count", 0) or 0):
            return tr("tray_sync_errors", count=activity["error_count"])
        return tr("tray_sync_idle")

    def _refresh_sync_status(self) -> None:
        if self.sync_status_item is None:
            return
        activity = get_sync_activity()
        self.sync_status_item.set_label(self._sync_status_label(activity))

    def _append_item(self, label: str, callback: Any) -> None:
        item = Gtk.MenuItem(label=label)
        item.connect("activate", callback)
        self.menu.append(item)

    def _append_icon_item(
        self,
        menu: Gtk.Menu,
        label: str,
        icon_name: str,
        callback: Any,
        profile_id: str,
    ) -> None:
        item = Gtk.ImageMenuItem(label=label)
        item.set_image(Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU))
        item.set_always_show_image(True)
        item.connect("activate", callback, profile_id)
        menu.append(item)

    def _append_separator(self) -> None:
        self.menu.append(Gtk.SeparatorMenuItem())

    def _mount_profile(self, _item: Gtk.Widget, profile_id: str) -> None:
        self.menu.popdown()
        if systemd_start(profile_id):
            set_profile_pending_unmount(profile_id, False)
            set_profile_desired_mounted(profile_id, True)
        GLib.timeout_add(
            1500,
            self._finish_manual_mount,
            profile_id,
            bool(load_settings().get("open_after_mount")),
        )

    def _unmount_profile(self, _item: Gtk.Widget, profile_id: str) -> None:
        self.menu.popdown()
        activity = get_sync_activity()
        state = activity.get("profiles", {}).get(profile_id, {})
        if not snapshot_allows_unmount(activity, profile_id):
            dialog = Gtk.MessageDialog(
                message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.NONE,
                text=tr("unmount_during_sync_title") if state.get("transfer_state") == "busy" else tr("unmount"),
            )
            dialog.format_secondary_text(
                tr("unmount_safety_unknown") if state.get("transfer_state") != "busy" else tr(
                    "unmount_during_sync_body",
                    active=int(state.get("active_count", 0)),
                    queued=int(state.get("queued_count", 0)),
                )
            )
            dialog.add_button(tr("cancel"), Gtk.ResponseType.CANCEL)
            dialog.add_button(tr("wait_and_unmount"), Gtk.ResponseType.APPLY)
            dialog.add_button(tr("unmount_anyway"), Gtk.ResponseType.OK)
            destructive = dialog.get_widget_for_response(Gtk.ResponseType.OK)
            if destructive is not None:
                destructive.get_style_context().add_class("destructive-action")
            dialog.connect(
                "response",
                self._on_unmount_warning_response,
                profile_id,
            )
            dialog.show_all()
            return
        self._perform_unmount(profile_id)

    def _on_unmount_warning_response(
        self,
        dialog: Gtk.Dialog,
        response: int,
        profile_id: str,
    ) -> None:
        dialog.destroy()
        if response == Gtk.ResponseType.APPLY:
            set_profile_desired_mounted(profile_id, False)
            set_profile_pending_unmount(profile_id, True)
            self._pending_unmount.add(profile_id)
        elif response == Gtk.ResponseType.OK:
            self._perform_unmount(profile_id, force=True)

    def _finish_pending_unmounts(self) -> None:
        if not self._pending_unmount:
            return
        activity = get_sync_activity()
        for profile_id in list(self._pending_unmount):
            if not snapshot_allows_unmount(activity, profile_id):
                continue
            self._perform_unmount(profile_id)

    def _perform_unmount(self, profile_id: str, *, force: bool = False) -> None:
        if not force and probe_transfer_state(profile_id) != "idle":
            notify(tr("app_title"), tr("unmount_safety_blocked", profile=profile_id))
            return
        self._pending_unmount.discard(profile_id)
        set_profile_pending_unmount(profile_id, False)
        set_profile_desired_mounted(profile_id, False)
        profile = list_profiles().get(profile_id)
        if not systemd_stop(profile_id):
            unmount_raw(profile_id)
        if profile:
            clear_mount_folder_icon(profile)
        GLib.timeout_add(900, self._refresh_once)

    def _open_profile(self, _button: Gtk.Widget, profile_id: str) -> None:
        self.menu.popdown()
        open_mount_dir(profile_id)

    def _show_log(self, _button: Gtk.Widget, profile_id: str) -> None:
        self.menu.popdown()
        path = log_file_for(profile_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        subprocess.Popen(
            ["xdg-open", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _open_app(self, *_args: object) -> None:
        subprocess.Popen(
            application_command("gui"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _open_comparison(self, *_args: object) -> None:
        if not load_settings().get("context_file_comparison", False):
            return
        subprocess.Popen(
            application_command("compare"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _quit_application(self, *_args: object) -> None:
        try:
            subprocess.run(
                ["gapplication", "action", APP_ID, "quit"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except FileNotFoundError:
            pass
        Gtk.main_quit()

    def _refresh_once(self) -> bool:
        self.refresh_menu()
        return False

    def _finish_manual_mount(self, profile_id: str, open_after_mount: bool) -> bool:
        profile = list_profiles().get(profile_id)
        if profile and is_profile_mounted(profile) and open_after_mount:
            open_mount_dir(profile_id)
        self.refresh_menu()
        return False

    def _mount_desired_profiles(self) -> None:
        profiles = list_profiles()
        for profile_id in sorted(desired_mounted_profiles()):
            profile = profiles.get(profile_id)
            if profile and not is_profile_mounted(profile):
                systemd_start(profile_id)

    def _remount_missing_desired_profiles(self) -> None:
        profiles = list_profiles()
        for profile_id in sorted(desired_mounted_profiles()):
            if profile_id in self._auto_remounting:
                continue

            profile = profiles.get(profile_id)
            if not profile or is_profile_mounted(profile):
                continue

            self._auto_remounting.add(profile_id)
            if systemd_is_active(profile_id):
                systemd_stop(profile_id)
            systemd_start(profile_id)
            GLib.timeout_add(2500, self._finish_auto_remount, profile_id)

    def _finish_auto_remount(self, profile_id: str) -> bool:
        self._auto_remounting.discard(profile_id)
        self.refresh_menu()
        return False


def _lock_file() -> object | None:
    ensure_runtime_dirs()
    path = runtime_dir() / "tray.lock"
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
    except OSError:
        return None
    try:
        os.fchmod(descriptor, 0o600)
    except OSError:
        os.close(descriptor)
        return None
    lock = os.fdopen(descriptor, "w", encoding="utf-8")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return None
    lock.write(str(os.getpid()))
    lock.flush()
    return lock


def run() -> int:
    if load_settings().get("disable_tray_icon"):
        return 0

    lock = _lock_file()
    if lock is None:
        return 0

    try:
        TrayApp()
    except Exception as exc:
        _tray_log(f"Tray failed: {exc}")
        print(f"talaryn tray failed: {exc}", file=sys.stderr)
        return 1

    Gtk.main()
    return 0
