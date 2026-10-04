from __future__ import annotations

from collections import deque
from datetime import datetime
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from . import __author__, __license__, __version__
from .activity_filter import activity_is_visible, normalize_activity_patterns
from .activity_grouping import activity_group_key
from .activity_store import ActivityStore
from .config import (
    delete_profiles,
    desired_mounted_profiles,
    list_profiles,
    load_settings,
    load_state,
    profiles_for_remote,
    save_settings,
    save_profile,
    set_profile_desired_mounted,
    set_profile_pending_unmount,
    update_sponsor_reminder_state,
)
from .i18n import SUPPORTED_LANGUAGES, tr
from .icons import (
    DEFAULT_REMOTE_ICON_NAME,
    OBJECT_STORAGE_REMOTE_ICON_NAME,
    WEBDAV_REMOTE_ICON_NAME,
    available_icon_names,
    icon_path,
    install_custom_icon,
    remote_icon_path,
)
from .mount import (
    is_profile_mounted,
    cache_dir_for,
    clear_logs_for,
    log_file_for,
    open_mount_dir,
    clear_mount_folder_icon,
    set_mount_folder_icon,
    unmount_raw,
    build_mount_command,
    command_file_for,
    disk_usage_for_path,
    format_command_for_display,
    human_size,
    show_file_in_nautilus,
    apply_bwlimit_to_running_mounts,
)
from .monitor import ensure_monitor_running
from .paths import APP_ICON_NAME, APP_ID, CACHE_DIR
from .rc import installed_rclone_version, queue_set_expiry
from .rclone import (
    build_remote_spec,
    continue_remote_config,
    create_remote_config,
    delete_remote_config,
    default_custom_command,
    detect_kind,
    icon_for_kind,
    list_remote_dirs,
    list_remotes,
    redacted_remote_config,
    remote_name_exists,
    remote_join,
    remote_parent,
    RcloneCommandResult,
    RcloneCommandRunner,
    RemoteConfigStep,
    sftp_no_hashcheck_command,
    ssh_development_command,
    test_remote_connection,
    update_remote_config,
)
from .systemd import start as systemd_start
from .systemd import is_active as systemd_is_active
from .systemd import stop as systemd_stop
from .sync_status import (
    add_activity_group_headers,
    activity_display_path,
    get_sync_activity,
    group_activity_events,
    merge_activity_group_summaries,
    write_sync_snapshot,
)
from .util import application_command, expand_path, read_tail, safe_id
from .transfer_safety import probe_transfer_state, snapshot_allows_unmount

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

try:
    gi.require_version("GnomeDesktop", "4.0")
    from gi.repository import GnomeDesktop  # type: ignore  # noqa: E402
except (ValueError, ImportError):
    GnomeDesktop = None  # type: ignore

DEFAULTS = {
    "vfs_cache_mode": "full",
    "vfs_cache_max_size": "10G",
    "vfs_cache_max_age": "24h",
    "dir_cache_time": "1h",
    "umask": "022",
}

MOUNT_CHECK_INITIAL_DELAY_MS = 1500
MOUNT_CHECK_RETRY_DELAY_MS = 1000
MOUNT_CHECK_MAX_ATTEMPTS = 30
ACTIVITY_HISTORY_PAGE_SIZE = 100
ACTIVITY_HISTORY_MEMORY_LIMIT = 5000
ACTIVITY_INITIAL_GROUP_LIMIT = 10
ACTIVITY_GROUP_CHILD_LIMIT = 250
ACTIVITY_CHILD_RENDER_BATCH_SIZE = 16
ACTIVITY_GROUP_RENDER_BATCH_SIZE = 12
ACTIVITY_THUMBNAIL_QUERY_LIMIT = 4
ACTIVITY_LIST_DEFAULT_HEIGHT = 360
ACTIVITY_LIST_MIN_HEIGHT = 240
ACTIVITY_LIST_MAX_HEIGHT = 840
ACTIVITY_LIST_HEIGHT_STEP = 120
SPONSOR_URL = "https://github.com/sponsors/k-krakowski"
SPONSOR_INITIAL_DELAY_SECONDS = 14 * 24 * 60 * 60
SPONSOR_REMIND_LATER_SECONDS = 7 * 24 * 60 * 60
SPONSOR_AFTER_VISIT_SECONDS = 90 * 24 * 60 * 60
SPEED_LIMIT_RE = re.compile(r"^(?:off|0|\d+(?:\.\d+)?[BKMGTP]?)$", re.IGNORECASE)
SPEED_LIMIT_VALUE_RE = re.compile(
    r"^(?P<value>\d+(?:\.\d+)?)(?P<unit>[BKMGTP]?)$",
    re.IGNORECASE,
)
SPEED_LIMIT_PRESETS = (
    "128K",
    "256K",
    "512K",
    "1M",
    "2M",
    "5M",
    "10M",
    "20M",
    "50M",
    "100M",
    "200M",
    "500M",
    "1G",
    "2G",
    "5G",
    "10G",
)


def _parsed_http_url(url: str):
    try:
        parsed = urlsplit(url)
        if parsed.scheme.casefold() not in {"http", "https"}:
            return None
        if not parsed.hostname:
            return None
        return parsed
    except ValueError:
        return None


def _webdav_credentials_are_transport_safe(url: str) -> bool:
    parsed = _parsed_http_url(url)
    if parsed is None:
        return False
    if parsed.scheme.casefold() == "https":
        return True
    hostname = (parsed.hostname or "").rstrip(".").casefold()
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False
CACHE_SIZE_PRESETS = (
    "1G",
    "2G",
    "5G",
    "10G",
    "20G",
    "50G",
    "100G",
    "200G",
    "500G",
    "1T",
)
CACHE_AGE_PRESETS = (
    "1h",
    "2h",
    "6h",
    "12h",
    "24h",
    "48h",
    "72h",
    "168h",
)
DIR_CACHE_TIME_PRESETS = (
    "10s",
    "30s",
    "1m",
    "5m",
    "30m",
    "1h",
    "6h",
    "12h",
    "24h",
    "48h",
    "72h",
)
VFS_CACHE_MODE_IDS = ("off", "minimal", "writes", "full")
UMASK_IDS = ("022", "002", "077", "007", "000", "custom")
LANGUAGE_IDS = ("system", *SUPPORTED_LANGUAGES)
LANGUAGE_LABEL_KEYS = {
    "system": "language_system",
    "pl": "language_polish",
    "en": "language_english",
    "ru": "language_russian",
}
COLOR_SCHEME_IDS = ("system", "light", "dark")
DOWNLOAD_CHART_COLOR = (0.20, 0.48, 0.88, 0.95)
UPLOAD_CHART_COLOR = (0.20, 0.72, 0.42, 0.95)


PRESETS = {
    "balanced": {
        "label_key": "preset_balanced",
        "description_key": "preset_balanced_help",
        "vfs_cache_mode": "full",
        "vfs_cache_max_size": "10G",
        "vfs_cache_max_age": "24h",
        "dir_cache_time": "1h",
        "umask": "022",
    },
    "large_files": {
        "label_key": "preset_large_files",
        "description_key": "preset_large_files_help",
        "vfs_cache_mode": "full",
        "vfs_cache_max_size": "50G",
        "vfs_cache_max_age": "72h",
        "dir_cache_time": "1h",
        "umask": "022",
    },
    "many_small_files": {
        "label_key": "preset_many_small_files",
        "description_key": "preset_many_small_files_help",
        "vfs_cache_mode": "full",
        "vfs_cache_max_size": "20G",
        "vfs_cache_max_age": "24h",
        "dir_cache_time": "6h",
        "umask": "022",
    },
    "ssh_development": {
        "label_key": "preset_ssh_development",
        "description_key": "preset_ssh_development_help",
        "vfs_cache_mode": "full",
        "vfs_cache_max_size": "20G",
        "vfs_cache_max_age": "2h",
        "dir_cache_time": "5m",
        "umask": "022",
        "custom_command": ssh_development_command,
    },
    "sftp_no_hashcheck": {
        "label_key": "preset_sftp_no_hashcheck",
        "description_key": "preset_sftp_no_hashcheck_help",
        "vfs_cache_mode": "full",
        "vfs_cache_max_size": "10G",
        "vfs_cache_max_age": "6h",
        "dir_cache_time": "30m",
        "umask": "022",
        "custom_command": sftp_no_hashcheck_command,
    },
    "low_cache": {
        "label_key": "preset_low_cache",
        "description_key": "preset_low_cache_help",
        "vfs_cache_mode": "writes",
        "vfs_cache_max_size": "2G",
        "vfs_cache_max_age": "6h",
        "dir_cache_time": "30m",
        "umask": "022",
    },
}


def _escape_pango_markup(value: object) -> str:
    """Return dynamic text that is safe in GTK properties using Pango markup."""
    return GLib.markup_escape_text(str(value or ""))


def _normalize_text_search_result(
    result: object,
) -> tuple[Any, Any] | None:
    """Normalize PyGObject text search results across GTK versions."""
    if result is None:
        return None
    try:
        start, end = result
    except (TypeError, ValueError):
        return None
    return start, end


def _speed_limit_bytes(value: str) -> float | None:
    match = SPEED_LIMIT_VALUE_RE.fullmatch(value.strip())
    if match is None:
        return None
    multipliers = {
        "": 1,
        "B": 1,
        "K": 1024,
        "M": 1024**2,
        "G": 1024**3,
        "T": 1024**4,
        "P": 1024**5,
    }
    return float(match.group("value")) * multipliers[
        match.group("unit").upper()
    ]


def _stepped_speed_limit(value: str, direction: int) -> str:
    text = value.strip()
    if not text or text.lower() in ("off", "0"):
        return SPEED_LIMIT_PRESETS[-1] if direction < 0 else ""

    current = _speed_limit_bytes(text)
    if current is None:
        return text
    presets = [
        (preset, _speed_limit_bytes(preset) or 0)
        for preset in SPEED_LIMIT_PRESETS
    ]
    if direction > 0:
        return next(
            (
                preset
                for preset, preset_bytes in presets
                if preset_bytes > current
            ),
            "",
        )
    return next(
        (
            preset
            for preset, preset_bytes in reversed(presets)
            if preset_bytes < current
        ),
        text,
    )


def _duration_seconds(value: str) -> float | None:
    match = re.fullmatch(
        r"(?P<value>\d+(?:\.\d+)?)(?P<unit>ms|s|m|h|d|w)",
        value.strip(),
        re.IGNORECASE,
    )
    if match is None:
        return None
    multipliers = {
        "ms": 0.001,
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
        "w": 604800,
    }
    return float(match.group("value")) * multipliers[
        match.group("unit").lower()
    ]


def _stepped_profile_value(
    value: str,
    direction: int,
    presets: tuple[str, ...],
    parser: Callable[[str], float | None],
) -> str:
    text = value.strip()
    current = parser(text)
    if current is None:
        return text
    parsed_presets = [
        (preset, parser(preset) or 0)
        for preset in presets
    ]
    if direction > 0:
        return next(
            (
                preset
                for preset, parsed_value in parsed_presets
                if parsed_value > current
            ),
            presets[-1],
        )
    return next(
        (
            preset
            for preset, parsed_value in reversed(parsed_presets)
            if parsed_value < current
        ),
        presets[0],
    )


def _apply_color_scheme(value: str) -> None:
    schemes = {
        "system": Adw.ColorScheme.DEFAULT,
        "light": Adw.ColorScheme.FORCE_LIGHT,
        "dark": Adw.ColorScheme.FORCE_DARK,
    }
    Adw.StyleManager.get_default().set_color_scheme(
        schemes.get(value, Adw.ColorScheme.DEFAULT)
    )


def _config_question_choices(
    examples: object,
) -> list[tuple[str, str]]:
    if not isinstance(examples, list):
        return []
    choices: list[tuple[str, str]] = []
    for example in examples:
        if not isinstance(example, dict):
            continue
        raw_value = example.get("Value")
        if raw_value is True:
            value = "true"
        elif raw_value is False:
            value = "false"
        elif raw_value is None:
            value = ""
        elif isinstance(raw_value, (list, dict)):
            value = json.dumps(raw_value, ensure_ascii=False)
        else:
            value = str(raw_value)
        help_text = str(example.get("Help", "") or "").strip()
        label = help_text.splitlines()[0] if help_text else value
        choices.append((label, value))
    return choices


def find_profile_target_conflict(
    profile_id: str,
    remote_spec: str,
    mount_dir: str,
) -> str | None:
    if not mount_dir.strip():
        return None

    normalized_mount_dir = expand_path(mount_dir)

    for existing_id, existing_profile in list_profiles().items():
        if existing_id == profile_id:
            continue

        existing_mount_dir = expand_path(str(existing_profile.get("mount_dir", "")))
        if existing_mount_dir != normalized_mount_dir:
            continue

        existing_remote_spec = str(existing_profile.get("remote_spec", ""))
        if existing_remote_spec == remote_spec:
            return tr("duplicate_profile_target", profile=existing_id)

        return tr("mount_dir_in_use", profile=existing_id)

    return None


def _hide_header_title_buttons(header: Adw.HeaderBar | Gtk.HeaderBar) -> None:
    if hasattr(header, "set_show_title_buttons"):
        header.set_show_title_buttons(False)
        return

    if hasattr(header, "set_show_start_title_buttons"):
        header.set_show_start_title_buttons(False)
    if hasattr(header, "set_show_end_title_buttons"):
        header.set_show_end_title_buttons(False)


def _activity_icon(icon_name: str, size: int) -> Gtk.Image:
    custom_icon = remote_icon_path(icon_name)
    if custom_icon:
        icon_file = Gio.File.new_for_path(custom_icon)
        image = Gtk.Image.new_from_gicon(Gio.FileIcon.new(icon_file))
    else:
        image = Gtk.Image(icon_name="folder-remote-symbolic")
    image.set_pixel_size(size)
    return image


class RemotePathBrowserDialog(Adw.Dialog):
    def __init__(
        self,
        parent: Gtk.Window,
        remote_name: str,
        initial_path: str,
        on_selected: Callable[[str], None],
    ) -> None:
        super().__init__()

        self.parent_window = parent
        self.remote_name = remote_name
        self.current_path = initial_path.strip()
        self.on_selected = on_selected
        self._browse_request_id = 0

        self.set_title(tr("browse_remote_title", remote=remote_name))
        self.set_content_width(720)
        self.set_content_height(520)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        _hide_header_title_buttons(header)

        cancel_button = Gtk.Button(label=tr("cancel"))
        cancel_button.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel_button)

        self.select_button = Gtk.Button(label=tr("select"))
        self.select_button.add_css_class("suggested-action")
        self.select_button.connect("clicked", self._on_select_clicked)
        header.pack_end(self.select_button)

        toolbar.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.set_spacing(10)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        path_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        path_box.set_hexpand(True)
        content.append(path_box)

        self.path_entry = Gtk.Entry()
        self.path_entry.set_hexpand(True)
        self.path_entry.set_width_chars(60)
        self.path_entry.set_text(self.current_path)
        path_box.append(self.path_entry)

        self.go_button = Gtk.Button(label=tr("go"))
        self.go_button.connect("clicked", self._on_go_clicked)
        path_box.append(self.go_button)

        self.up_button = Gtk.Button(label=tr("up"))
        self.up_button.connect("clicked", self._on_up_clicked)
        path_box.append(self.up_button)

        self.refresh_button = Gtk.Button(label=tr("refresh"))
        self.refresh_button.connect("clicked", self._on_refresh_clicked)
        path_box.append(self.refresh_button)

        self.info_label = Gtk.Label(xalign=0)
        self.info_label.set_wrap(True)
        self.info_label.add_css_class("dim-label")
        content.append(self.info_label)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_hexpand(True)
        scrolled.set_vexpand(True)
        content.append(scrolled)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.connect("row-activated", self._on_row_activated)
        scrolled.set_child(self.listbox)

        toolbar.set_content(content)
        self.set_child(toolbar)

        self._load_path(self.current_path)

    def get_selected_path(self) -> str:
        return self.current_path.strip()

    def _clear_listbox(self) -> None:
        child = self.listbox.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.listbox.remove(child)
            child = next_child

    def _load_path(self, path: str) -> None:
        path = path.strip()
        self.current_path = path
        self.path_entry.set_text(path)
        self._browse_request_id += 1
        request_id = self._browse_request_id

        self._clear_listbox()

        remote_spec = build_remote_spec(self.remote_name, path)
        self.info_label.set_text(
            f"{remote_spec}\n{tr('remote_browse_loading')}"
        )
        for widget in (
            self.select_button,
            self.go_button,
            self.up_button,
            self.refresh_button,
        ):
            widget.set_sensitive(False)

        threading.Thread(
            target=self._load_path_worker,
            args=(request_id, path),
            daemon=True,
        ).start()

    def _load_path_worker(self, request_id: int, path: str) -> None:
        dirs, error = list_remote_dirs(self.remote_name, path)
        GLib.idle_add(
            self._finish_load_path,
            request_id,
            path,
            dirs,
            error,
        )

    def _finish_load_path(
        self,
        request_id: int,
        path: str,
        dirs: list[str],
        error: str,
    ) -> bool:
        if request_id != self._browse_request_id:
            return False

        self.info_label.set_text(build_remote_spec(self.remote_name, path))
        for widget in (
            self.select_button,
            self.go_button,
            self.up_button,
            self.refresh_button,
        ):
            widget.set_sensitive(True)

        if error:
            row = Gtk.ListBoxRow()
            label = Gtk.Label(
                label=tr("remote_browse_failed", error=error),
                xalign=0,
            )
            label.set_wrap(True)
            label.set_margin_top(8)
            label.set_margin_bottom(8)
            label.set_margin_start(8)
            label.set_margin_end(8)
            row.set_child(label)
            row.set_sensitive(False)
            self.listbox.append(row)
            return False

        if not dirs:
            row = Gtk.ListBoxRow()
            label = Gtk.Label(
                label=tr("remote_path_empty"),
                xalign=0,
            )
            label.set_wrap(True)
            label.set_margin_top(8)
            label.set_margin_bottom(8)
            label.set_margin_start(8)
            label.set_margin_end(8)
            row.set_child(label)
            row.set_sensitive(False)
            self.listbox.append(row)
            return False

        for dirname in dirs:
            row = Gtk.ListBoxRow()
            row.remote_dir_name = dirname  # type: ignore[attr-defined]

            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row_box.set_margin_top(8)
            row_box.set_margin_bottom(8)
            row_box.set_margin_start(8)
            row_box.set_margin_end(8)

            icon = Gtk.Image(icon_name="folder-symbolic")
            icon.set_pixel_size(18)
            row_box.append(icon)

            label = Gtk.Label(label=dirname, xalign=0)
            label.set_hexpand(True)
            label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            row_box.append(label)

            row.set_child(row_box)
            self.listbox.append(row)
        return False

    def _on_select_clicked(self, _button: Gtk.Button) -> None:
        self.on_selected(self.get_selected_path())
        self.close()

    def _on_row_activated(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        dirname = getattr(row, "remote_dir_name", "")
        if not dirname:
            return

        new_path = remote_join(self.current_path, dirname)
        self._load_path(new_path)

    def _on_go_clicked(self, _button: Gtk.Button) -> None:
        self._load_path(self.path_entry.get_text().strip())

    def _on_up_clicked(self, _button: Gtk.Button) -> None:
        self._load_path(remote_parent(self.current_path))

    def _on_refresh_clicked(self, _button: Gtk.Button) -> None:
        self._load_path(self.current_path)


class RemoteConfigDialog(Adw.Dialog):
    PROVIDER_IDS = (
        "onedrive",
        "drive",
        "dropbox",
        "pcloud",
        "box",
        "protondrive",
        "mega",
        "iclouddrive",
        "seafile",
        "webdav",
        "smb",
        "b2",
        "s3",
        "google cloud storage",
        "azureblob",
        "azurefiles",
        "sftp",
        "ftp",
    )
    OAUTH_PROVIDER_IDS = (
        "onedrive",
        "drive",
        "dropbox",
        "pcloud",
        "box",
        "google cloud storage",
    )
    ONEDRIVE_REGION_IDS = ("global", "us", "de", "cn")
    GOOGLE_SCOPE_IDS = (
        "drive",
        "drive.readonly",
        "drive.file",
        "drive.appfolder",
        "drive.metadata.readonly",
    )
    PCLOUD_HOST_IDS = ("api.pcloud.com", "eapi.pcloud.com")
    BOX_SUBTYPE_IDS = ("user", "enterprise")
    ICLOUD_SERVICE_IDS = ("drive", "photos")
    GOOGLE_CLOUD_AUTH_IDS = (
        "oauth",
        "service_account",
        "environment",
        "anonymous",
    )
    AZURE_AUTH_IDS = (
        "account_key",
        "sas_url",
        "connection_string",
        "environment",
        "service_principal",
    )
    WEBDAV_VENDOR_IDS = (
        "fastmail",
        "nextcloud",
        "owncloud",
        "infinitescale",
        "sharepoint",
        "sharepoint-ntlm",
        "rclone",
        "other",
    )
    WEBDAV_TILE_VENDOR_IDS = (
        "fastmail",
        "nextcloud",
        "owncloud",
        "sharepoint",
    )
    WEBDAV_GENERIC_VENDOR_IDS = (
        "infinitescale",
        "sharepoint-ntlm",
        "rclone",
        "other",
    )
    WEBDAV_GENERIC_VENDOR_LABEL_KEYS = (
        "remote_webdav_infinitescale",
        "remote_webdav_sharepoint_ntlm",
        "remote_webdav_rclone",
        "remote_webdav_other",
    )
    WEBDAV_AUTH_IDS = ("password", "bearer", "none")
    S3_PROVIDER_IDS = (
        "AWS",
        "Alibaba",
        "ArvanCloud",
        "BizflyCloud",
        "Ceph",
        "ChinaMobile",
        "Cloudflare",
        "Cubbit",
        "DigitalOcean",
        "Dreamhost",
        "Exaba",
        "Fastly",
        "FileLu",
        "FlashBlade",
        "GCS",
        "HCP",
        "Minio",
        "Hetzner",
        "HuaweiOBS",
        "IBMCOS",
        "IDrive",
        "ImpossibleCloud",
        "Intercolo",
        "IONOS",
        "Leviia",
        "Liara",
        "Linode",
        "LyveCloud",
        "Magalu",
        "Mega",
        "Netease",
        "Outscale",
        "OVHcloud",
        "Petabox",
        "Qiniu",
        "Rabata",
        "RackCorp",
        "Rclone",
        "Scaleway",
        "SeaweedFS",
        "Selectel",
        "Servercore",
        "SpectraLogic",
        "Storj",
        "Synology",
        "TencentCOS",
        "US3",
        "Wasabi",
        "Zadara",
        "Zata",
        "Other",
    )
    S3_PROVIDER_LABELS = (
        "Amazon Web Services (AWS) S3",
        "Alibaba Cloud OSS",
        "Arvan Cloud Object Storage",
        "Bizfly Cloud Simple Storage",
        "Ceph Object Storage",
        "China Mobile EOS",
        "Cloudflare R2",
        "Cubbit DS3",
        "DigitalOcean Spaces",
        "DreamHost DreamObjects",
        "Exaba Object Storage",
        "Fastly Object Storage",
        "FileLu S5",
        "Pure Storage FlashBlade",
        "Google Cloud Storage",
        "Hitachi Content Platform",
        "MinIO",
        "Hetzner Object Storage",
        "Huawei Object Storage",
        "IBM Cloud Object Storage",
        "IDrive e2",
        "Impossible Cloud",
        "Intercolo Object Storage",
        "IONOS Cloud",
        "Leviia Object Storage",
        "Liara Object Storage",
        "Linode Object Storage",
        "Seagate Lyve Cloud",
        "Magalu Object Storage",
        "MEGA S4",
        "Netease Object Storage",
        "OUTSCALE Object Storage",
        "OVHcloud Object Storage",
        "Petabox Object Storage",
        "Qiniu Kodo",
        "Rabata Cloud Storage",
        "RackCorp Object Storage",
        "Rclone S3 Server",
        "Scaleway Object Storage",
        "SeaweedFS S3",
        "Selectel Object Storage",
        "Servercore Object Storage",
        "Spectra Logic BlackPearl",
        "Storj S3 Gateway",
        "Synology C2 Object Storage",
        "Tencent Cloud COS",
        "UCloud US3",
        "Wasabi",
        "Zadara Object Storage",
        "Zata S3 Gateway",
        "Other S3-compatible provider",
    )
    S3_TILE_PROVIDER_IDS = ("AWS", "Cloudflare", "Wasabi")
    SFTP_AUTH_IDS = ("agent", "key", "password", "external")
    FTP_SECURITY_IDS = ("explicit_tls", "implicit_tls", "plain")
    PROVIDER_TILE_GROUPS = (
        (
            "remote_drive_type_cloud",
            (
                (
                    "onedrive",
                    "remote_provider_onedrive",
                    DEFAULT_REMOTE_ICON_NAME,
                    "onedrive",
                    "",
                ),
                (
                    "drive",
                    "remote_provider_google_drive",
                    DEFAULT_REMOTE_ICON_NAME,
                    "google-drive",
                    "",
                ),
                (
                    "dropbox",
                    "remote_provider_dropbox",
                    DEFAULT_REMOTE_ICON_NAME,
                    "dropbox",
                    "",
                ),
                (
                    "pcloud",
                    "remote_provider_pcloud",
                    DEFAULT_REMOTE_ICON_NAME,
                    "pcloud",
                    "",
                ),
                (
                    "box",
                    "remote_provider_box",
                    DEFAULT_REMOTE_ICON_NAME,
                    "box",
                    "",
                ),
                (
                    "protondrive",
                    "remote_provider_protondrive",
                    DEFAULT_REMOTE_ICON_NAME,
                    "proton-drive",
                    "",
                ),
                (
                    "mega",
                    "remote_provider_mega",
                    DEFAULT_REMOTE_ICON_NAME,
                    "mega",
                    "",
                ),
                (
                    "iclouddrive",
                    "remote_provider_icloud",
                    DEFAULT_REMOTE_ICON_NAME,
                    "icloud",
                    "",
                ),
                (
                    "seafile",
                    "remote_provider_seafile",
                    DEFAULT_REMOTE_ICON_NAME,
                    "seafile",
                    "",
                ),
            ),
        ),
        (
            "remote_drive_type_webdav",
            (
                (
                    "webdav",
                    "remote_provider_fastmail",
                    WEBDAV_REMOTE_ICON_NAME,
                    "fastmail",
                    "fastmail",
                ),
                (
                    "webdav",
                    "remote_provider_nextcloud",
                    WEBDAV_REMOTE_ICON_NAME,
                    "nextcloud",
                    "nextcloud",
                ),
                (
                    "webdav",
                    "remote_provider_owncloud",
                    WEBDAV_REMOTE_ICON_NAME,
                    "owncloud",
                    "owncloud",
                ),
                (
                    "webdav",
                    "remote_provider_sharepoint",
                    WEBDAV_REMOTE_ICON_NAME,
                    "sharepoint",
                    "sharepoint",
                ),
                (
                    "webdav",
                    "remote_provider_webdav",
                    WEBDAV_REMOTE_ICON_NAME,
                    "webdav",
                    "other",
                ),
            ),
        ),
        (
            "remote_drive_type_object",
            (
                (
                    "b2",
                    "remote_provider_b2",
                    OBJECT_STORAGE_REMOTE_ICON_NAME,
                    "backblaze-b2",
                    "",
                ),
                (
                    "s3",
                    "remote_provider_amazon_s3",
                    OBJECT_STORAGE_REMOTE_ICON_NAME,
                    "amazon-s3",
                    "AWS",
                ),
                (
                    "s3",
                    "remote_provider_cloudflare_r2",
                    OBJECT_STORAGE_REMOTE_ICON_NAME,
                    "cloudflare-r2",
                    "Cloudflare",
                ),
                (
                    "s3",
                    "remote_provider_wasabi",
                    OBJECT_STORAGE_REMOTE_ICON_NAME,
                    "wasabi",
                    "Wasabi",
                ),
                (
                    "google cloud storage",
                    "remote_provider_google_cloud_storage",
                    OBJECT_STORAGE_REMOTE_ICON_NAME,
                    "google-cloud-storage",
                    "",
                ),
                (
                    "azureblob",
                    "remote_provider_azure_blob",
                    OBJECT_STORAGE_REMOTE_ICON_NAME,
                    "azure-blob",
                    "",
                ),
                (
                    "azurefiles",
                    "remote_provider_azure_files",
                    OBJECT_STORAGE_REMOTE_ICON_NAME,
                    "azure-files",
                    "",
                ),
                (
                    "s3",
                    "remote_provider_s3",
                    OBJECT_STORAGE_REMOTE_ICON_NAME,
                    "s3",
                    "Other",
                ),
            ),
        ),
        (
            "remote_drive_type_servers",
            (
                (
                    "sftp",
                    "remote_provider_sftp",
                    "sftp.svg",
                    "sftp",
                    "",
                ),
                (
                    "ftp",
                    "remote_provider_ftp",
                    "ftp.svg",
                    "ftp",
                    "",
                ),
                (
                    "smb",
                    "remote_provider_smb",
                    "smb.svg",
                    "smb",
                    "",
                ),
            ),
        ),
    )

    def __init__(
        self,
        parent: Gtk.Window,
        on_created: Callable[[str], None],
    ) -> None:
        super().__init__()

        self.parent_window = parent
        self.on_created = on_created
        self.runner = RcloneCommandRunner()
        self.remote_name = ""
        self.remote_type = ""
        self.base_parameters: dict[str, str] = {}
        self.current_config_state = ""
        self.question_widget: Gtk.Widget | None = None
        self.question_values: list[str | None] = []
        self._last_suggested_name = ""
        self._operation_serial = 0
        self._operation_running = False
        self._config_started = False
        self._remote_delivered = False
        self._cleanup_started = False
        self._closed = False
        self._ftp_plain_confirmed = False
        self.editing_existing = False
        self.question_custom_entry: Gtk.Entry | None = None
        self.locked_webdav_vendor = ""
        self.locked_s3_provider = ""

        self.set_title(tr("remote_config_title"))
        self.set_content_width(780)
        self.set_content_height(720)
        self.set_can_close(False)

        self._build_ui()
        self._update_provider_fields()

    def _build_ui(self) -> None:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        _hide_header_title_buttons(header)

        self.cancel_button = Gtk.Button(label=tr("cancel"))
        self.cancel_button.connect("clicked", self._on_cancel_clicked)
        header.pack_start(self.cancel_button)

        self.back_button = Gtk.Button(icon_name="go-previous-symbolic")
        self.back_button.set_tooltip_text(tr("back"))
        self.back_button.connect("clicked", self._on_back_clicked)
        header.pack_start(self.back_button)

        self.primary_button = Gtk.Button(label=tr("remote_config_create"))
        self.primary_button.add_css_class("suggested-action")
        self.primary_button.connect("clicked", self._on_primary_clicked)
        header.pack_end(self.primary_button)

        toolbar.add_top_bar(header)

        self.page_stack = Gtk.Stack()
        self.page_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.page_stack.set_vexpand(True)
        toolbar.set_content(self.page_stack)
        self.set_child(toolbar)
        self.set_default_widget(self.primary_button)

        self._build_provider_page()
        self._build_name_page()
        self._build_setup_page()
        self._build_progress_page()
        self._build_result_page()
        self._configure_combo_rows()
        self._show_wizard_page("provider")

    def _build_provider_page(self) -> None:
        provider_model = Gtk.StringList()
        for key in (
            "remote_provider_onedrive",
            "remote_provider_google_drive",
            "remote_provider_dropbox",
            "remote_provider_pcloud",
            "remote_provider_box",
            "remote_provider_protondrive",
            "remote_provider_mega",
            "remote_provider_icloud",
            "remote_provider_seafile",
            "remote_provider_webdav",
            "remote_provider_smb",
            "remote_provider_b2",
            "remote_provider_s3",
            "remote_provider_google_cloud_storage",
            "remote_provider_azure_blob",
            "remote_provider_azure_files",
            "remote_provider_sftp",
            "remote_provider_ftp",
        ):
            provider_model.append(tr(key))

        # This row remains the single source of selection state for creation
        # and editing, while the new connection wizard presents visual tiles.
        self.provider_combo = Adw.ComboRow(
            title=tr("remote_config_provider"),
            model=provider_model,
        )
        self.provider_combo.set_selected(0)

        page = Adw.PreferencesPage()
        page.set_title(tr("remote_config_choose_provider"))
        page.add(
            Adw.PreferencesGroup(
                title=tr("remote_config_choose_provider"),
                description=tr("remote_config_choose_provider_help"),
            )
        )
        for category_key, tiles in self.PROVIDER_TILE_GROUPS:
            group = Adw.PreferencesGroup(title=tr(category_key))
            page.add(group)

            grid = Gtk.FlowBox()
            grid.set_selection_mode(Gtk.SelectionMode.NONE)
            grid.set_homogeneous(True)
            grid.set_min_children_per_line(2)
            grid.set_max_children_per_line(5)
            grid.set_row_spacing(12)
            grid.set_column_spacing(12)
            grid.set_margin_top(6)
            grid.set_margin_bottom(6)
            group.add(grid)

            for provider, label_key, icon_name, suggestion, vendor in tiles:
                button = self._provider_tile(
                    tr(label_key),
                    icon_name,
                )
                button.connect(
                    "clicked",
                    self._on_provider_tile_clicked,
                    provider,
                    label_key,
                    icon_name,
                    suggestion,
                    vendor,
                    category_key,
                )
                grid.append(button)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_child(page)
        self.page_stack.add_named(scrolled, "provider")

    def _provider_tile(self, label: str, icon_name: str) -> Gtk.Button:
        button = Gtk.Button()
        button.add_css_class("card")
        button.set_size_request(126, 108)
        button.set_hexpand(True)

        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
        )
        content.set_margin_top(12)
        content.set_margin_bottom(10)
        content.set_margin_start(8)
        content.set_margin_end(8)
        content.append(_activity_icon(icon_name, 48))

        label_widget = Gtk.Label(label=label)
        label_widget.set_wrap(True)
        label_widget.set_justify(Gtk.Justification.CENTER)
        label_widget.set_max_width_chars(18)
        content.append(label_widget)
        button.set_child(content)
        return button

    def _build_name_page(self) -> None:
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            title=tr("remote_config_name_step"),
            description=tr("remote_config_name_help"),
        )
        page.add(group)

        self.selected_provider_row = Adw.ActionRow(
            title=tr("remote_provider_onedrive"),
            subtitle=tr("remote_drive_type_cloud"),
        )
        self.selected_provider_icon = Gtk.Image()
        self.selected_provider_icon.set_pixel_size(34)
        self.selected_provider_row.add_prefix(self.selected_provider_icon)
        self._set_selected_provider_icon(DEFAULT_REMOTE_ICON_NAME)
        group.add(self.selected_provider_row)

        self.remote_name_entry = self._entry_row(tr("remote_config_name"))
        self.remote_name_entry.set_text("onedrive")
        self._last_suggested_name = "onedrive"
        group.add(self.remote_name_entry)

        self.page_stack.add_named(page, "name")

    def _build_setup_page(self) -> None:
        page = Adw.PreferencesPage()

        self.onedrive_group = self._build_onedrive_group()
        self.google_drive_group = self._build_google_drive_group()
        self.dropbox_group = self._build_dropbox_group()
        self.pcloud_group = self._build_pcloud_group()
        self.box_group = self._build_box_group()
        self.proton_group = self._build_proton_group()
        self.mega_group = self._build_mega_group()
        self.icloud_group = self._build_icloud_group()
        self.seafile_group = self._build_seafile_group()
        self.webdav_group = self._build_webdav_group()
        self.smb_group = self._build_smb_group()
        self.b2_group = self._build_b2_group()
        self.s3_group = self._build_s3_group()
        self.google_cloud_group = self._build_google_cloud_group()
        self.azure_fields: dict[str, dict[str, Gtk.Widget]] = {}
        self.azure_blob_group = self._build_azure_group(
            "azureblob",
            "remote_provider_azure_blob",
        )
        self.azure_files_group = self._build_azure_group(
            "azurefiles",
            "remote_provider_azure_files",
            include_share=True,
        )
        self.sftp_group = self._build_sftp_group()
        self.ftp_group = self._build_ftp_group()

        for group in (
            self.onedrive_group,
            self.google_drive_group,
            self.dropbox_group,
            self.pcloud_group,
            self.box_group,
            self.proton_group,
            self.mega_group,
            self.icloud_group,
            self.seafile_group,
            self.webdav_group,
            self.smb_group,
            self.b2_group,
            self.s3_group,
            self.google_cloud_group,
            self.azure_blob_group,
            self.azure_files_group,
            self.sftp_group,
            self.ftp_group,
        ):
            page.add(group)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_child(page)
        self.page_stack.add_named(scrolled, "setup")

    def _build_onedrive_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=tr("remote_provider_onedrive"),
            description=tr("remote_oauth_help"),
        )

        self.onedrive_client_id_entry = self._entry_row(
            tr("remote_client_id")
        )
        self.onedrive_client_secret_entry = self._password_row(
            tr("remote_client_secret")
        )

        model = Gtk.StringList()
        for key in (
            "remote_region_global",
            "remote_region_us",
            "remote_region_de",
            "remote_region_cn",
        ):
            model.append(tr(key))
        self.onedrive_region_combo = Adw.ComboRow(
            title=tr("remote_onedrive_region"),
            model=model,
        )
        self.onedrive_region_combo.set_selected(0)
        self.onedrive_tenant_entry = self._entry_row(
            tr("remote_onedrive_tenant")
        )

        for row in (
            self.onedrive_client_id_entry,
            self.onedrive_client_secret_entry,
            self.onedrive_region_combo,
            self.onedrive_tenant_entry,
        ):
            group.add(row)
        return group

    def _build_google_drive_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=tr("remote_provider_google_drive"),
            description=tr("remote_oauth_help"),
        )

        self.google_client_id_entry = self._entry_row(tr("remote_client_id"))
        self.google_client_secret_entry = self._password_row(
            tr("remote_client_secret")
        )

        scope_model = Gtk.StringList()
        for key in (
            "remote_google_scope_full",
            "remote_google_scope_readonly",
            "remote_google_scope_created",
            "remote_google_scope_appfolder",
            "remote_google_scope_metadata",
        ):
            scope_model.append(tr(key))
        self.google_scope_combo = Adw.ComboRow(
            title=tr("remote_google_scope"),
            model=scope_model,
        )
        self.google_scope_combo.set_selected(0)

        self.google_service_account_entry = self._entry_row(
            tr("remote_google_service_account")
        )
        service_account_button = self._suffix_button(
            "document-open-symbolic",
            tr("browse"),
        )
        service_account_button.connect(
            "clicked",
            self._on_service_account_browse_clicked,
        )
        self.google_service_account_entry.add_suffix(service_account_button)

        for row in (
            self.google_client_id_entry,
            self.google_client_secret_entry,
            self.google_scope_combo,
            self.google_service_account_entry,
        ):
            group.add(row)
        return group

    def _build_dropbox_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=tr("remote_provider_dropbox"),
            description=tr("remote_oauth_help"),
        )
        self.dropbox_client_id_entry = self._entry_row(
            tr("remote_client_id")
        )
        self.dropbox_client_secret_entry = self._password_row(
            tr("remote_client_secret")
        )
        group.add(self.dropbox_client_id_entry)
        group.add(self.dropbox_client_secret_entry)
        return group

    def _build_pcloud_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=tr("remote_provider_pcloud"),
            description=tr("remote_oauth_help"),
        )
        self.pcloud_client_id_entry = self._entry_row(
            tr("remote_client_id")
        )
        self.pcloud_client_secret_entry = self._password_row(
            tr("remote_client_secret")
        )

        region_model = Gtk.StringList()
        region_model.append(tr("remote_pcloud_region_us"))
        region_model.append(tr("remote_pcloud_region_eu"))
        self.pcloud_region_combo = Adw.ComboRow(
            title=tr("remote_pcloud_region"),
            model=region_model,
        )
        self.pcloud_region_combo.set_selected(0)

        group.add(self.pcloud_client_id_entry)
        group.add(self.pcloud_client_secret_entry)
        group.add(self.pcloud_region_combo)
        return group

    def _build_box_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=tr("remote_provider_box"),
            description=tr("remote_box_help"),
        )
        self.box_client_id_entry = self._entry_row(tr("remote_client_id"))
        self.box_client_secret_entry = self._password_row(
            tr("remote_client_secret")
        )

        subtype_model = Gtk.StringList()
        subtype_model.append(tr("remote_box_user"))
        subtype_model.append(tr("remote_box_enterprise"))
        self.box_subtype_combo = Adw.ComboRow(
            title=tr("remote_box_account_type"),
            model=subtype_model,
        )
        self.box_subtype_combo.set_selected(0)
        self.box_subtype_combo.connect(
            "notify::selected",
            self._on_box_subtype_changed,
        )

        self.box_config_file_entry = self._entry_row(
            tr("remote_box_config_file")
        )
        box_config_button = self._suffix_button(
            "document-open-symbolic",
            tr("browse"),
        )
        box_config_button.connect(
            "clicked",
            self._on_box_config_browse_clicked,
        )
        self.box_config_file_entry.add_suffix(box_config_button)
        self.box_root_folder_entry = self._entry_row(
            tr("remote_box_root_folder")
        )

        for row in (
            self.box_client_id_entry,
            self.box_client_secret_entry,
            self.box_subtype_combo,
            self.box_config_file_entry,
            self.box_root_folder_entry,
        ):
            group.add(row)
        return group

    def _build_proton_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=tr("remote_provider_protondrive"),
            description=tr("remote_proton_help"),
        )
        self.proton_user_entry = self._entry_row(tr("remote_user"))
        self.proton_password_entry = self._password_row(
            tr("remote_password")
        )
        self.proton_2fa_entry = self._entry_row(tr("remote_2fa_code"))
        self.proton_otp_secret_entry = self._password_row(
            tr("remote_otp_secret")
        )
        for row in (
            self.proton_user_entry,
            self.proton_password_entry,
            self.proton_2fa_entry,
            self.proton_otp_secret_entry,
        ):
            group.add(row)
        return group

    def _build_mega_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=tr("remote_provider_mega"),
            description=tr("remote_mega_help"),
        )
        self.mega_user_entry = self._entry_row(tr("remote_user"))
        self.mega_password_entry = self._password_row(
            tr("remote_password")
        )
        self.mega_2fa_entry = self._entry_row(tr("remote_2fa_code"))
        for row in (
            self.mega_user_entry,
            self.mega_password_entry,
            self.mega_2fa_entry,
        ):
            group.add(row)
        return group

    def _build_icloud_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=tr("remote_provider_icloud"),
            description=tr("remote_icloud_help"),
        )
        service_model = Gtk.StringList()
        service_model.append(tr("remote_icloud_drive"))
        service_model.append(tr("remote_icloud_photos"))
        self.icloud_service_combo = Adw.ComboRow(
            title=tr("remote_icloud_service"),
            model=service_model,
        )
        self.icloud_service_combo.set_selected(0)
        self.icloud_apple_id_entry = self._entry_row(
            tr("remote_icloud_apple_id")
        )
        self.icloud_password_entry = self._password_row(
            tr("remote_password")
        )
        for row in (
            self.icloud_service_combo,
            self.icloud_apple_id_entry,
            self.icloud_password_entry,
        ):
            group.add(row)
        return group

    def _build_seafile_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=tr("remote_provider_seafile"),
            description=tr("remote_seafile_help"),
        )
        self.seafile_url_entry = self._entry_row(tr("remote_seafile_url"))
        self.seafile_url_entry.set_text("https://cloud.seafile.com/")
        self.seafile_user_entry = self._entry_row(tr("remote_user"))
        self.seafile_password_entry = self._password_row(
            tr("remote_password")
        )
        self.seafile_2fa_row = Adw.SwitchRow(
            title=tr("remote_seafile_2fa"),
            subtitle=tr("remote_seafile_2fa_help"),
        )
        self.seafile_library_entry = self._entry_row(
            tr("remote_seafile_library")
        )
        self.seafile_library_key_entry = self._password_row(
            tr("remote_seafile_library_key")
        )
        self.seafile_token_entry = self._password_row(
            tr("remote_seafile_auth_token")
        )
        for row in (
            self.seafile_url_entry,
            self.seafile_user_entry,
            self.seafile_password_entry,
            self.seafile_2fa_row,
            self.seafile_library_entry,
            self.seafile_library_key_entry,
            self.seafile_token_entry,
        ):
            group.add(row)
        return group

    def _build_google_cloud_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=tr("remote_provider_google_cloud_storage"),
            description=tr("remote_google_cloud_help"),
        )
        auth_model = Gtk.StringList()
        for key in (
            "remote_google_cloud_auth_oauth",
            "remote_google_cloud_auth_service_account",
            "remote_google_cloud_auth_environment",
            "remote_google_cloud_auth_anonymous",
        ):
            auth_model.append(tr(key))
        self.google_cloud_auth_combo = Adw.ComboRow(
            title=tr("remote_authentication"),
            model=auth_model,
        )
        self.google_cloud_auth_combo.set_selected(0)
        self.google_cloud_auth_combo.connect(
            "notify::selected",
            self._on_google_cloud_auth_changed,
        )
        self.google_cloud_client_id_entry = self._entry_row(
            tr("remote_client_id")
        )
        self.google_cloud_client_secret_entry = self._password_row(
            tr("remote_client_secret")
        )
        self.google_cloud_project_entry = self._entry_row(
            tr("remote_google_cloud_project_number")
        )
        self.google_cloud_service_account_entry = self._entry_row(
            tr("remote_google_service_account")
        )
        service_account_button = self._suffix_button(
            "document-open-symbolic",
            tr("browse"),
        )
        service_account_button.connect(
            "clicked",
            lambda _button: self._open_file_dialog(
                tr("remote_google_service_account"),
                self.google_cloud_service_account_entry,
            ),
        )
        self.google_cloud_service_account_entry.add_suffix(
            service_account_button
        )
        for row in (
            self.google_cloud_auth_combo,
            self.google_cloud_client_id_entry,
            self.google_cloud_client_secret_entry,
            self.google_cloud_project_entry,
            self.google_cloud_service_account_entry,
        ):
            group.add(row)
        return group

    def _build_azure_group(
        self,
        provider: str,
        title_key: str,
        include_share: bool = False,
    ) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=tr(title_key),
            description=tr("remote_azure_help"),
        )
        auth_model = Gtk.StringList()
        for key in (
            "remote_azure_auth_account_key",
            "remote_azure_auth_sas",
            "remote_azure_auth_connection_string",
            "remote_azure_auth_environment",
            "remote_azure_auth_service_principal",
        ):
            auth_model.append(tr(key))
        auth_combo = Adw.ComboRow(
            title=tr("remote_authentication"),
            model=auth_model,
        )
        auth_combo.set_selected(0)
        account_entry = self._entry_row(tr("remote_azure_account"))
        key_entry = self._password_row(tr("remote_azure_key"))
        sas_entry = self._password_row(tr("remote_azure_sas_url"))
        connection_entry = self._password_row(
            tr("remote_azure_connection_string")
        )
        tenant_entry = self._entry_row(tr("remote_azure_tenant"))
        client_id_entry = self._entry_row(tr("remote_client_id"))
        client_secret_entry = self._password_row(
            tr("remote_client_secret")
        )
        share_entry = self._entry_row(tr("remote_azure_share_name"))

        fields: dict[str, Gtk.Widget] = {
            "auth": auth_combo,
            "account": account_entry,
            "key": key_entry,
            "sas_url": sas_entry,
            "connection_string": connection_entry,
            "tenant": tenant_entry,
            "client_id": client_id_entry,
            "client_secret": client_secret_entry,
            "share_name": share_entry,
        }
        self.azure_fields[provider] = fields
        auth_combo.connect(
            "notify::selected",
            lambda _combo, _pspec, selected_provider=provider: (
                self._update_azure_auth_fields(selected_provider)
            ),
        )
        rows = [
            auth_combo,
            account_entry,
            key_entry,
            sas_entry,
            connection_entry,
            tenant_entry,
            client_id_entry,
            client_secret_entry,
        ]
        if include_share:
            rows.append(share_entry)
        for row in rows:
            group.add(row)
        return group

    def _build_webdav_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=tr("remote_provider_webdav"),
            description=tr("remote_webdav_help"),
        )
        self.webdav_url_entry = self._entry_row(tr("remote_webdav_url"))

        vendor_model = Gtk.StringList()
        self.webdav_vendor_ids = self.WEBDAV_GENERIC_VENDOR_IDS
        for key in self.WEBDAV_GENERIC_VENDOR_LABEL_KEYS:
            vendor_model.append(tr(key))
        self.webdav_vendor_combo = Adw.ComboRow(
            title=tr("remote_webdav_vendor"),
            model=vendor_model,
        )
        self.webdav_vendor_combo.set_selected(
            self.webdav_vendor_ids.index("other")
        )

        self.webdav_user_entry = self._entry_row(tr("remote_user"))
        auth_model = Gtk.StringList()
        for key in (
            "remote_webdav_auth_password",
            "remote_webdav_auth_bearer",
            "remote_webdav_auth_none",
        ):
            auth_model.append(tr(key))
        self.webdav_auth_combo = Adw.ComboRow(
            title=tr("remote_webdav_auth"),
            model=auth_model,
        )
        self.webdav_auth_combo.set_selected(0)
        self.webdav_auth_combo.connect(
            "notify::selected",
            self._on_webdav_auth_changed,
        )
        self.webdav_password_entry = self._password_row(
            tr("remote_password")
        )
        self.webdav_bearer_entry = self._password_row(
            tr("remote_webdav_bearer_token")
        )

        for row in (
            self.webdav_url_entry,
            self.webdav_vendor_combo,
            self.webdav_user_entry,
            self.webdav_auth_combo,
            self.webdav_password_entry,
            self.webdav_bearer_entry,
        ):
            group.add(row)
        return group

    def _build_smb_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=tr("remote_provider_smb"),
            description=tr("remote_smb_help"),
        )
        self.smb_host_entry = self._entry_row(tr("remote_host"))
        self.smb_user_entry = self._entry_row(tr("remote_user"))
        self.smb_port_entry = self._entry_row(tr("remote_port"))
        self.smb_port_entry.set_text("445")
        self.smb_port_entry.set_input_purpose(Gtk.InputPurpose.DIGITS)
        self.smb_password_entry = self._password_row(tr("remote_password"))
        self.smb_domain_entry = self._entry_row(tr("remote_smb_domain"))
        self.smb_domain_entry.set_text("WORKGROUP")
        self.smb_kerberos_row = Adw.SwitchRow(
            title=tr("remote_smb_kerberos"),
            subtitle=tr("remote_smb_kerberos_help"),
        )
        self.smb_kerberos_row.connect(
            "notify::active",
            self._on_smb_auth_changed,
        )
        self.smb_spn_entry = self._entry_row(tr("remote_smb_spn"))

        for row in (
            self.smb_host_entry,
            self.smb_user_entry,
            self.smb_port_entry,
            self.smb_password_entry,
            self.smb_domain_entry,
            self.smb_kerberos_row,
            self.smb_spn_entry,
        ):
            group.add(row)
        return group

    def _build_b2_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=tr("remote_provider_b2"),
            description=tr("remote_b2_help"),
        )
        self.b2_account_entry = self._entry_row(tr("remote_b2_account"))
        self.b2_key_entry = self._password_row(tr("remote_b2_key"))
        self.b2_hard_delete_row = Adw.SwitchRow(
            title=tr("remote_b2_hard_delete"),
            subtitle=tr("remote_b2_hard_delete_help"),
        )
        for row in (
            self.b2_account_entry,
            self.b2_key_entry,
            self.b2_hard_delete_row,
        ):
            group.add(row)
        return group

    def _build_s3_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=tr("remote_provider_s3"),
            description=tr("remote_s3_help"),
        )
        provider_model = Gtk.StringList()
        self.s3_provider_ids = tuple(
            provider
            for provider in self.S3_PROVIDER_IDS
            if provider not in self.S3_TILE_PROVIDER_IDS
        )
        provider_labels = dict(
            zip(self.S3_PROVIDER_IDS, self.S3_PROVIDER_LABELS)
        )
        for provider in self.s3_provider_ids:
            provider_model.append(
                tr("remote_s3_other_provider")
                if provider == "Other"
                else provider_labels[provider]
            )
        self.s3_provider_combo = Adw.ComboRow(
            title=tr("remote_s3_provider"),
            model=provider_model,
        )
        self.s3_provider_combo.set_selected(
            self.s3_provider_ids.index("Other")
        )

        self.s3_env_auth_row = Adw.SwitchRow(
            title=tr("remote_s3_env_auth"),
            subtitle=tr("remote_s3_env_auth_help"),
        )
        self.s3_env_auth_row.connect(
            "notify::active",
            self._on_s3_auth_changed,
        )
        self.s3_access_key_entry = self._entry_row(
            tr("remote_s3_access_key")
        )
        self.s3_secret_key_entry = self._password_row(
            tr("remote_s3_secret_key")
        )
        self.s3_endpoint_entry = self._entry_row(
            tr("remote_s3_endpoint")
        )
        self.s3_region_entry = self._entry_row(tr("remote_s3_region"))

        for row in (
            self.s3_provider_combo,
            self.s3_env_auth_row,
            self.s3_access_key_entry,
            self.s3_secret_key_entry,
            self.s3_endpoint_entry,
            self.s3_region_entry,
        ):
            group.add(row)
        return group

    def _build_sftp_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=tr("remote_provider_sftp"),
            description=tr("remote_sftp_help"),
        )

        self.sftp_host_entry = self._entry_row(tr("remote_host"))
        self.sftp_user_entry = self._entry_row(tr("remote_user"))
        self.sftp_port_entry = self._entry_row(tr("remote_port"))
        self.sftp_port_entry.set_text("22")
        self.sftp_port_entry.set_input_purpose(Gtk.InputPurpose.DIGITS)

        auth_model = Gtk.StringList()
        for key in (
            "remote_sftp_auth_agent",
            "remote_sftp_auth_key",
            "remote_sftp_auth_password",
            "remote_sftp_auth_external",
        ):
            auth_model.append(tr(key))
        self.sftp_auth_combo = Adw.ComboRow(
            title=tr("remote_sftp_auth"),
            model=auth_model,
        )
        self.sftp_auth_combo.set_selected(0)
        self.sftp_auth_combo.connect(
            "notify::selected",
            self._on_sftp_auth_changed,
        )

        self.sftp_password_entry = self._password_row(tr("remote_password"))
        self.sftp_key_file_entry = self._entry_row(tr("remote_sftp_key_file"))
        key_file_button = self._suffix_button(
            "document-open-symbolic",
            tr("browse"),
        )
        key_file_button.connect(
            "clicked",
            self._on_sftp_key_browse_clicked,
        )
        self.sftp_key_file_entry.add_suffix(key_file_button)
        self.sftp_key_pass_entry = self._password_row(
            tr("remote_sftp_key_password")
        )
        self.sftp_ssh_command_entry = self._entry_row(
            tr("remote_sftp_ssh_command")
        )

        self.sftp_verify_host_row = Adw.SwitchRow(
            title=tr("remote_sftp_verify_host"),
            subtitle=tr("remote_sftp_verify_host_help"),
        )
        self.sftp_verify_host_row.set_active(True)
        self.sftp_verify_host_row.connect(
            "notify::active",
            self._on_sftp_verify_changed,
        )
        self.sftp_known_hosts_entry = self._entry_row(
            tr("remote_sftp_known_hosts")
        )
        self.sftp_known_hosts_entry.set_text(
            str(Path.home() / ".ssh" / "known_hosts")
        )
        known_hosts_button = self._suffix_button(
            "document-open-symbolic",
            tr("browse"),
        )
        known_hosts_button.connect(
            "clicked",
            self._on_sftp_known_hosts_browse_clicked,
        )
        self.sftp_known_hosts_entry.add_suffix(known_hosts_button)

        for row in (
            self.sftp_host_entry,
            self.sftp_user_entry,
            self.sftp_port_entry,
            self.sftp_auth_combo,
            self.sftp_password_entry,
            self.sftp_key_file_entry,
            self.sftp_key_pass_entry,
            self.sftp_ssh_command_entry,
            self.sftp_verify_host_row,
            self.sftp_known_hosts_entry,
        ):
            group.add(row)
        return group

    def _build_ftp_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=tr("remote_provider_ftp"),
            description=tr("remote_ftp_help"),
        )

        self.ftp_host_entry = self._entry_row(tr("remote_host"))
        self.ftp_user_entry = self._entry_row(tr("remote_user"))
        self.ftp_port_entry = self._entry_row(tr("remote_port"))
        self.ftp_port_entry.set_text("21")
        self.ftp_port_entry.set_input_purpose(Gtk.InputPurpose.DIGITS)
        self.ftp_password_entry = self._password_row(tr("remote_password"))

        security_model = Gtk.StringList()
        for key in (
            "remote_ftp_explicit_tls",
            "remote_ftp_implicit_tls",
            "remote_ftp_plain",
        ):
            security_model.append(tr(key))
        self.ftp_security_combo = Adw.ComboRow(
            title=tr("remote_ftp_security"),
            model=security_model,
        )
        self.ftp_security_combo.set_selected(0)
        self.ftp_security_combo.connect(
            "notify::selected",
            self._on_ftp_security_changed,
        )

        self.ftp_verify_certificate_row = Adw.SwitchRow(
            title=tr("remote_ftp_verify_certificate"),
            subtitle=tr("remote_ftp_verify_certificate_help"),
        )
        self.ftp_verify_certificate_row.set_active(True)

        for row in (
            self.ftp_host_entry,
            self.ftp_user_entry,
            self.ftp_port_entry,
            self.ftp_password_entry,
            self.ftp_security_combo,
            self.ftp_verify_certificate_row,
        ):
            group.add(row)
        return group

    def _build_progress_page(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)
        box.set_margin_start(36)
        box.set_margin_end(36)

        self.progress_spinner = Gtk.Spinner()
        self.progress_spinner.set_size_request(48, 48)
        box.append(self.progress_spinner)

        self.progress_title = Gtk.Label()
        self.progress_title.add_css_class("title-2")
        self.progress_title.set_wrap(True)
        self.progress_title.set_justify(Gtk.Justification.CENTER)
        box.append(self.progress_title)

        self.progress_body = Gtk.Label()
        self.progress_body.add_css_class("dim-label")
        self.progress_body.set_wrap(True)
        self.progress_body.set_justify(Gtk.Justification.CENTER)
        self.progress_body.set_max_width_chars(68)
        box.append(self.progress_body)

        self.page_stack.add_named(box, "progress")

    def _build_result_page(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)
        box.set_margin_start(36)
        box.set_margin_end(36)

        self.result_icon = Gtk.Image(icon_name="dialog-warning-symbolic")
        self.result_icon.set_pixel_size(48)
        box.append(self.result_icon)

        self.result_title = Gtk.Label()
        self.result_title.add_css_class("title-2")
        self.result_title.set_wrap(True)
        self.result_title.set_justify(Gtk.Justification.CENTER)
        box.append(self.result_title)

        self.result_error = Gtk.Label()
        self.result_error.add_css_class("dim-label")
        self.result_error.set_wrap(True)
        self.result_error.set_selectable(True)
        self.result_error.set_max_width_chars(76)
        box.append(self.result_error)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_halign(Gtk.Align.CENTER)
        actions.set_margin_top(12)
        box.append(actions)

        self.retry_button = Gtk.Button(label=tr("remote_test_retry"))
        self.retry_button.add_css_class("suggested-action")
        self.retry_button.connect(
            "clicked",
            lambda *_: self._start_connection_test(),
        )
        actions.append(self.retry_button)

        self.use_anyway_button = Gtk.Button(label=tr("remote_use_anyway"))
        self.use_anyway_button.connect(
            "clicked",
            lambda *_: self._finish_success(),
        )
        actions.append(self.use_anyway_button)

        self.remove_button = Gtk.Button(label=tr("remote_remove_and_return"))
        self.remove_button.add_css_class("destructive-action")
        self.remove_button.connect(
            "clicked",
            self._on_remove_and_return_clicked,
        )
        actions.append(self.remove_button)

        self.page_stack.add_named(box, "result")

    def _configure_combo_rows(self) -> None:
        widgets: list[Gtk.Widget] = [self.page_stack]
        while widgets:
            widget = widgets.pop()
            if isinstance(widget, Adw.ComboRow):
                text_width = self._combo_row_text_width(widget)
                selected_width = min(max(text_width + 16, 150), 420)
                list_width = min(max(text_width + 24, 190), 620)
                widget.set_factory(
                    self._combo_row_factory(
                        selected=True,
                        width=selected_width,
                    )
                )
                widget.set_list_factory(
                    self._combo_row_factory(
                        selected=False,
                        width=list_width,
                    )
                )

            child = widget.get_first_child()
            while child is not None:
                widgets.append(child)
                child = child.get_next_sibling()

    @staticmethod
    def _combo_row_text_width(combo: Adw.ComboRow) -> int:
        model = combo.get_model()
        if model is None:
            return 0
        width = 0
        for index in range(model.get_n_items()):
            item = model.get_item(index)
            if not isinstance(item, Gtk.StringObject):
                continue
            layout = combo.create_pango_layout(item.get_string())
            item_width, _height = layout.get_pixel_size()
            width = max(width, item_width)
        return width

    @staticmethod
    def _combo_row_factory(
        selected: bool,
        width: int,
    ) -> Gtk.SignalListItemFactory:
        factory = Gtk.SignalListItemFactory()

        def setup(
            _factory: Gtk.SignalListItemFactory,
            item: Gtk.ListItem,
        ) -> None:
            label = Gtk.Label(xalign=1.0 if selected else 0.0)
            label.set_wrap(True)
            label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            label.set_ellipsize(Pango.EllipsizeMode.NONE)
            label.set_size_request(width, -1)
            label.set_justify(
                Gtk.Justification.RIGHT
                if selected
                else Gtk.Justification.LEFT
            )
            label.set_margin_start(8)
            label.set_margin_end(8)
            label.set_margin_top(5)
            label.set_margin_bottom(5)
            item.set_child(label)

        def bind(
            _factory: Gtk.SignalListItemFactory,
            item: Gtk.ListItem,
        ) -> None:
            value = item.get_item()
            label = item.get_child()
            if not isinstance(value, Gtk.StringObject):
                return
            if not isinstance(label, Gtk.Label):
                return
            text = value.get_string()
            label.set_text(text)
            label.set_tooltip_text(text)

        factory.connect("setup", setup)
        factory.connect("bind", bind)
        return factory

    def _entry_row(self, title: str) -> Adw.EntryRow:
        row = Adw.EntryRow(title=title)
        row.set_activates_default(True)
        return row

    def _password_row(self, title: str) -> Adw.PasswordEntryRow:
        row = Adw.PasswordEntryRow(title=title)
        row.set_activates_default(True)
        return row

    def _suffix_button(self, icon_name: str, tooltip: str) -> Gtk.Button:
        button = Gtk.Button(icon_name=icon_name)
        button.add_css_class("flat")
        button.set_valign(Gtk.Align.CENTER)
        button.set_tooltip_text(tooltip)
        return button

    def _selected_id(self, combo: Adw.ComboRow, ids: tuple[str, ...]) -> str:
        selected = combo.get_selected()
        return ids[selected] if selected < len(ids) else ids[0]

    def _selected_webdav_vendor(self) -> str:
        return self.locked_webdav_vendor or self._selected_id(
            self.webdav_vendor_combo,
            self.webdav_vendor_ids,
        )

    def _selected_s3_provider(self) -> str:
        return self.locked_s3_provider or self._selected_id(
            self.s3_provider_combo,
            self.s3_provider_ids,
        )

    def _set_selected_provider_icon(self, icon_name: str) -> None:
        custom_icon = remote_icon_path(icon_name)
        if custom_icon:
            icon_file = Gio.File.new_for_path(custom_icon)
            self.selected_provider_icon.set_from_gicon(
                Gio.FileIcon.new(icon_file)
            )
        else:
            self.selected_provider_icon.set_from_icon_name(
                "folder-remote-symbolic"
            )

    def _on_provider_tile_clicked(
        self,
        _button: Gtk.Button,
        provider: str,
        label_key: str,
        icon_name: str,
        suggestion: str,
        provider_preset: str,
        category_key: str,
    ) -> None:
        try:
            self.provider_combo.set_selected(
                self.PROVIDER_IDS.index(provider)
            )
        except ValueError:
            return

        current = self.remote_name_entry.get_text().strip()
        if not current or current == self._last_suggested_name:
            self.remote_name_entry.set_text(suggestion)
        self._last_suggested_name = suggestion

        self.locked_webdav_vendor = ""
        self.locked_s3_provider = ""
        if provider == "webdav" and provider_preset:
            if provider_preset in self.WEBDAV_TILE_VENDOR_IDS:
                self.locked_webdav_vendor = provider_preset
            else:
                try:
                    self.webdav_vendor_combo.set_selected(
                        self.webdav_vendor_ids.index(provider_preset)
                    )
                except ValueError:
                    pass
        elif provider == "s3" and provider_preset:
            if provider_preset in self.S3_TILE_PROVIDER_IDS:
                self.locked_s3_provider = provider_preset
            else:
                try:
                    self.s3_provider_combo.set_selected(
                        self.s3_provider_ids.index(provider_preset)
                    )
                except ValueError:
                    pass

        self.selected_provider_row.set_title(tr(label_key))
        self.selected_provider_row.set_subtitle(tr(category_key))
        self._set_selected_provider_icon(icon_name)
        self._update_provider_fields()
        self._show_wizard_page("name")
        GLib.idle_add(self._focus_remote_name)

    def _focus_remote_name(self) -> bool:
        self.remote_name_entry.grab_focus()
        return False

    def _show_wizard_page(self, page_name: str) -> None:
        self.page_stack.set_visible_child_name(page_name)
        if page_name == "provider":
            self.back_button.set_visible(False)
            self.primary_button.set_visible(False)
        elif page_name == "name":
            self.back_button.set_visible(not self.editing_existing)
            self.primary_button.set_label(tr("continue"))
            self.primary_button.set_visible(True)
            self.primary_button.set_sensitive(True)
        elif page_name == "setup":
            self.back_button.set_visible(not self.editing_existing)
            self.primary_button.set_label(
                tr("save")
                if self.editing_existing
                else tr("remote_config_create")
            )
            self.primary_button.set_visible(True)
            self.primary_button.set_sensitive(True)

    def _on_back_clicked(self, _button: Gtk.Button) -> None:
        if self.editing_existing:
            return
        page_name = self.page_stack.get_visible_child_name()
        if page_name == "setup":
            self._show_wizard_page("name")
        elif page_name == "name":
            self._show_wizard_page("provider")

    def _update_provider_fields(self) -> None:
        provider = self._selected_id(self.provider_combo, self.PROVIDER_IDS)
        for provider_id, group in (
            ("onedrive", self.onedrive_group),
            ("drive", self.google_drive_group),
            ("dropbox", self.dropbox_group),
            ("pcloud", self.pcloud_group),
            ("box", self.box_group),
            ("protondrive", self.proton_group),
            ("mega", self.mega_group),
            ("iclouddrive", self.icloud_group),
            ("seafile", self.seafile_group),
            ("webdav", self.webdav_group),
            ("smb", self.smb_group),
            ("b2", self.b2_group),
            ("s3", self.s3_group),
            ("google cloud storage", self.google_cloud_group),
            ("azureblob", self.azure_blob_group),
            ("azurefiles", self.azure_files_group),
            ("sftp", self.sftp_group),
            ("ftp", self.ftp_group),
        ):
            group.set_visible(provider == provider_id)
        self.webdav_vendor_combo.set_visible(
            provider == "webdav" and not self.locked_webdav_vendor
        )
        self.s3_provider_combo.set_visible(
            provider == "s3" and not self.locked_s3_provider
        )
        self._update_box_fields()
        self._update_webdav_auth_fields()
        self._update_smb_auth_fields()
        self._update_s3_auth_fields()
        self._update_google_cloud_auth_fields()
        self._update_azure_auth_fields("azureblob")
        self._update_azure_auth_fields("azurefiles")
        self._update_sftp_auth_fields()
        self._update_ftp_security_fields()

    def _on_google_cloud_auth_changed(
        self,
        _combo: Adw.ComboRow,
        _pspec: object,
    ) -> None:
        self._update_google_cloud_auth_fields()

    def _update_google_cloud_auth_fields(self) -> None:
        auth = self._selected_id(
            self.google_cloud_auth_combo,
            self.GOOGLE_CLOUD_AUTH_IDS,
        )
        oauth = auth == "oauth"
        self.google_cloud_client_id_entry.set_visible(oauth)
        self.google_cloud_client_secret_entry.set_visible(oauth)
        self.google_cloud_service_account_entry.set_visible(
            auth == "service_account"
        )
        self.google_cloud_project_entry.set_visible(auth != "anonymous")

    def _update_azure_auth_fields(self, provider: str) -> None:
        fields = self.azure_fields.get(provider)
        if fields is None:
            return
        auth_combo = fields["auth"]
        if not isinstance(auth_combo, Adw.ComboRow):
            return
        auth = self._selected_id(auth_combo, self.AZURE_AUTH_IDS)
        fields["account"].set_visible(
            auth in ("account_key", "service_principal")
        )
        fields["key"].set_visible(auth == "account_key")
        fields["sas_url"].set_visible(auth == "sas_url")
        fields["connection_string"].set_visible(
            auth == "connection_string"
        )
        service_principal = auth == "service_principal"
        fields["tenant"].set_visible(service_principal)
        fields["client_id"].set_visible(service_principal)
        fields["client_secret"].set_visible(service_principal)

    def _on_box_subtype_changed(
        self,
        _combo: Adw.ComboRow,
        _pspec: object,
    ) -> None:
        self._update_box_fields()

    def _update_box_fields(self) -> None:
        subtype = self._selected_id(
            self.box_subtype_combo,
            self.BOX_SUBTYPE_IDS,
        )
        self.box_config_file_entry.set_visible(subtype == "enterprise")

    def _on_webdav_auth_changed(
        self,
        _combo: Adw.ComboRow,
        _pspec: object,
    ) -> None:
        self._update_webdav_auth_fields()

    def _update_webdav_auth_fields(self) -> None:
        auth = self._selected_id(
            self.webdav_auth_combo,
            self.WEBDAV_AUTH_IDS,
        )
        self.webdav_user_entry.set_visible(auth == "password")
        self.webdav_password_entry.set_visible(auth == "password")
        self.webdav_bearer_entry.set_visible(auth == "bearer")

    def _on_smb_auth_changed(
        self,
        _row: Adw.SwitchRow,
        _pspec: object,
    ) -> None:
        self._update_smb_auth_fields()

    def _update_smb_auth_fields(self) -> None:
        kerberos = self.smb_kerberos_row.get_active()
        self.smb_password_entry.set_visible(not kerberos)
        self.smb_spn_entry.set_visible(kerberos)

    def _on_s3_auth_changed(
        self,
        _row: Adw.SwitchRow,
        _pspec: object,
    ) -> None:
        self._update_s3_auth_fields()

    def _update_s3_auth_fields(self) -> None:
        manual_credentials = not self.s3_env_auth_row.get_active()
        self.s3_access_key_entry.set_visible(manual_credentials)
        self.s3_secret_key_entry.set_visible(manual_credentials)

    def _on_sftp_auth_changed(
        self,
        _combo: Adw.ComboRow,
        _pspec: object,
    ) -> None:
        self._update_sftp_auth_fields()

    def _update_sftp_auth_fields(self) -> None:
        auth = self._selected_id(self.sftp_auth_combo, self.SFTP_AUTH_IDS)
        uses_internal_client = auth != "external"
        self.sftp_host_entry.set_visible(uses_internal_client)
        self.sftp_user_entry.set_visible(uses_internal_client)
        self.sftp_port_entry.set_visible(uses_internal_client)
        self.sftp_password_entry.set_visible(auth == "password")
        self.sftp_key_file_entry.set_visible(auth == "key")
        self.sftp_key_pass_entry.set_visible(auth == "key")
        self.sftp_ssh_command_entry.set_visible(auth == "external")
        self.sftp_verify_host_row.set_visible(uses_internal_client)
        self.sftp_known_hosts_entry.set_visible(
            uses_internal_client and self.sftp_verify_host_row.get_active()
        )

    def _on_sftp_verify_changed(
        self,
        _row: Adw.SwitchRow,
        _pspec: object,
    ) -> None:
        auth = self._selected_id(self.sftp_auth_combo, self.SFTP_AUTH_IDS)
        self.sftp_known_hosts_entry.set_visible(
            auth != "external" and self.sftp_verify_host_row.get_active()
        )

    def _on_ftp_security_changed(
        self,
        _combo: Adw.ComboRow,
        _pspec: object,
    ) -> None:
        self._ftp_plain_confirmed = False
        self._update_ftp_security_fields()

    def _update_ftp_security_fields(self) -> None:
        security = self._selected_id(
            self.ftp_security_combo,
            self.FTP_SECURITY_IDS,
        )
        self.ftp_verify_certificate_row.set_visible(security != "plain")
        if security == "implicit_tls" and (
            not self.ftp_port_entry.get_text().strip()
            or self.ftp_port_entry.get_text().strip() == "21"
        ):
            self.ftp_port_entry.set_text("990")
        elif security != "implicit_tls" and (
            not self.ftp_port_entry.get_text().strip()
            or self.ftp_port_entry.get_text().strip() == "990"
        ):
            self.ftp_port_entry.set_text("21")

    def _on_service_account_browse_clicked(self, _button: Gtk.Button) -> None:
        self._open_file_dialog(
            tr("remote_google_service_account"),
            self.google_service_account_entry,
        )

    def _on_box_config_browse_clicked(self, _button: Gtk.Button) -> None:
        self._open_file_dialog(
            tr("remote_box_config_file"),
            self.box_config_file_entry,
        )

    def _on_sftp_key_browse_clicked(self, _button: Gtk.Button) -> None:
        self._open_file_dialog(
            tr("remote_sftp_key_file"),
            self.sftp_key_file_entry,
        )

    def _on_sftp_known_hosts_browse_clicked(self, _button: Gtk.Button) -> None:
        self._open_file_dialog(
            tr("remote_sftp_known_hosts"),
            self.sftp_known_hosts_entry,
        )

    def _open_file_dialog(self, title: str, target: Adw.EntryRow) -> None:
        dialog = Gtk.FileDialog.new()
        dialog.set_title(title)
        dialog.set_modal(True)
        dialog.open(
            self.parent_window,
            None,
            self._on_file_selected,
            target,
        )

    def _on_file_selected(
        self,
        dialog: Gtk.FileDialog,
        result: Gio.AsyncResult,
        target: Adw.EntryRow,
    ) -> None:
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return
        path = file.get_path()
        if path:
            target.set_text(path)

    def _on_primary_clicked(self, _button: Gtk.Button) -> None:
        page_name = self.page_stack.get_visible_child_name()
        if page_name == "name":
            if self._validated_remote_name() is not None:
                self._show_wizard_page("setup")
        elif page_name == "setup":
            self._validate_and_start_creation()
        elif page_name == "question":
            self._submit_question_answer()

    def _validated_remote_name(self) -> str | None:
        remote_name = self.remote_name_entry.get_text().strip().rstrip(":")
        if not remote_name:
            self._show_error(tr("remote_name_required"))
            return None
        if (
            remote_name.startswith("-")
            or any(char in remote_name for char in (":", "/", "\\", "\n"))
        ):
            self._show_error(tr("remote_name_invalid"))
            return None
        if remote_name_exists(remote_name):
            self._show_error(tr("remote_name_exists", name=remote_name))
            return None
        return remote_name

    def _validate_and_start_creation(self) -> None:
        remote_name = self._validated_remote_name()
        if remote_name is None:
            return

        provider = self._selected_id(self.provider_combo, self.PROVIDER_IDS)
        parameters, validation_error = self._collect_parameters(provider)
        if validation_error:
            self._show_error(validation_error)
            return

        if (
            provider == "ftp"
            and self._selected_id(
                self.ftp_security_combo,
                self.FTP_SECURITY_IDS,
            )
            == "plain"
            and not self._ftp_plain_confirmed
        ):
            self._confirm_plain_ftp()
            return

        self.remote_name = remote_name
        self.remote_type = provider
        self.base_parameters = parameters
        self._config_started = True
        self._show_progress(
            tr("remote_config_creating"),
            (
                tr("remote_oauth_wait")
                if provider in self.OAUTH_PROVIDER_IDS
                else tr("remote_config_please_wait")
            ),
        )
        self._run_async(
            lambda: create_remote_config(
                self.remote_name,
                self.remote_type,
                self.base_parameters,
                self.runner,
            ),
            self._on_config_step,
        )

    def _collect_parameters(
        self,
        provider: str,
    ) -> tuple[dict[str, str], str]:
        if provider == "onedrive":
            return (
                {
                    "client_id": self.onedrive_client_id_entry.get_text().strip(),
                    "client_secret": (
                        self.onedrive_client_secret_entry.get_text().strip()
                    ),
                    "region": self._selected_id(
                        self.onedrive_region_combo,
                        self.ONEDRIVE_REGION_IDS,
                    ),
                    "tenant": self.onedrive_tenant_entry.get_text().strip(),
                },
                "",
            )

        if provider == "drive":
            service_account = (
                self.google_service_account_entry.get_text().strip()
            )
            if service_account and not Path(expand_path(service_account)).is_file():
                return {}, tr("remote_file_not_found", path=service_account)
            return (
                {
                    "client_id": self.google_client_id_entry.get_text().strip(),
                    "client_secret": (
                        self.google_client_secret_entry.get_text().strip()
                    ),
                    "scope": self._selected_id(
                        self.google_scope_combo,
                        self.GOOGLE_SCOPE_IDS,
                    ),
                    "service_account_file": service_account,
                },
                "",
            )

        if provider == "dropbox":
            return (
                {
                    "client_id": self.dropbox_client_id_entry.get_text().strip(),
                    "client_secret": (
                        self.dropbox_client_secret_entry.get_text().strip()
                    ),
                },
                "",
            )

        if provider == "pcloud":
            return (
                {
                    "client_id": self.pcloud_client_id_entry.get_text().strip(),
                    "client_secret": (
                        self.pcloud_client_secret_entry.get_text().strip()
                    ),
                    "hostname": self._selected_id(
                        self.pcloud_region_combo,
                        self.PCLOUD_HOST_IDS,
                    ),
                },
                "",
            )

        if provider == "box":
            subtype = self._selected_id(
                self.box_subtype_combo,
                self.BOX_SUBTYPE_IDS,
            )
            box_config = (
                self.box_config_file_entry.get_text().strip()
                if subtype == "enterprise"
                else ""
            )
            if box_config and not Path(expand_path(box_config)).is_file():
                return {}, tr("remote_file_not_found", path=box_config)
            if (
                subtype == "enterprise"
                and not box_config
                and not self.editing_existing
            ):
                return {}, tr("remote_box_config_required")
            return (
                {
                    "client_id": self.box_client_id_entry.get_text().strip(),
                    "client_secret": (
                        self.box_client_secret_entry.get_text().strip()
                    ),
                    "box_sub_type": subtype,
                    "box_config_file": box_config,
                    "root_folder_id": (
                        self.box_root_folder_entry.get_text().strip()
                    ),
                },
                "",
            )

        if provider == "protondrive":
            username = self.proton_user_entry.get_text().strip()
            password = self.proton_password_entry.get_text()
            if not username:
                return {}, tr("remote_user_required")
            if not password and not self.editing_existing:
                return {}, tr("remote_password_required")
            parameters = {
                "username": username,
                "2fa": self.proton_2fa_entry.get_text().strip(),
                "otp_secret_key": (
                    self.proton_otp_secret_entry.get_text().strip()
                ),
            }
            if password:
                parameters["password"] = password
            return parameters, ""

        if provider == "mega":
            username = self.mega_user_entry.get_text().strip()
            password = self.mega_password_entry.get_text()
            if not username:
                return {}, tr("remote_user_required")
            if not password and not self.editing_existing:
                return {}, tr("remote_password_required")
            parameters = {
                "user": username,
                "2fa": self.mega_2fa_entry.get_text().strip(),
            }
            if password:
                parameters["pass"] = password
            return parameters, ""

        if provider == "iclouddrive":
            apple_id = self.icloud_apple_id_entry.get_text().strip()
            password = self.icloud_password_entry.get_text()
            if not apple_id:
                return {}, tr("remote_icloud_apple_id_required")
            if not password and not self.editing_existing:
                return {}, tr("remote_password_required")
            parameters = {
                "service": self._selected_id(
                    self.icloud_service_combo,
                    self.ICLOUD_SERVICE_IDS,
                ),
                "apple_id": apple_id,
            }
            if password:
                parameters["password"] = password
            return parameters, ""

        if provider == "seafile":
            url = self.seafile_url_entry.get_text().strip()
            username = self.seafile_user_entry.get_text().strip()
            password = self.seafile_password_entry.get_text()
            auth_token = self.seafile_token_entry.get_text().strip()
            if not url:
                return {}, tr("remote_seafile_url_required")
            if not url.casefold().startswith(("https://", "http://")):
                return {}, tr("remote_webdav_url_invalid")
            if not username:
                return {}, tr("remote_user_required")
            if (
                not self.editing_existing
                and not password
                and not auth_token
            ):
                return {}, tr("remote_seafile_auth_required")
            parameters = {
                "url": url,
                "user": username,
                "2fa": (
                    "true" if self.seafile_2fa_row.get_active() else "false"
                ),
                "library": self.seafile_library_entry.get_text().strip(),
                "library_key": (
                    self.seafile_library_key_entry.get_text().strip()
                ),
            }
            if password:
                parameters["pass"] = password
            if auth_token:
                parameters["auth_token"] = auth_token
            return parameters, ""

        if provider == "webdav":
            url = self.webdav_url_entry.get_text().strip()
            if not url:
                return {}, tr("remote_webdav_url_required")
            if _parsed_http_url(url) is None:
                return {}, tr("remote_webdav_url_invalid")
            parameters = {
                "url": url,
                "vendor": self._selected_webdav_vendor(),
            }
            auth = self._selected_id(
                self.webdav_auth_combo,
                self.WEBDAV_AUTH_IDS,
            )
            if (
                auth in {"password", "bearer"}
                and not _webdav_credentials_are_transport_safe(url)
            ):
                return {}, tr("remote_webdav_insecure_auth")
            if auth == "password":
                password = self.webdav_password_entry.get_text()
                if not password and not self.editing_existing:
                    return {}, tr("remote_password_required")
                parameters["user"] = self.webdav_user_entry.get_text().strip()
                if password:
                    parameters["pass"] = password
            elif auth == "bearer":
                bearer = self.webdav_bearer_entry.get_text()
                if not bearer and not self.editing_existing:
                    return {}, tr("remote_bearer_token_required")
                if bearer:
                    parameters["bearer_token"] = bearer
            return parameters, ""

        if provider == "smb":
            host = self.smb_host_entry.get_text().strip()
            if not host and not self.editing_existing:
                return {}, tr("remote_host_required")
            port, error = self._validated_port(self.smb_port_entry.get_text())
            if error:
                return {}, error
            kerberos = self.smb_kerberos_row.get_active()
            parameters = {
                "host": host,
                "user": self.smb_user_entry.get_text().strip(),
                "port": port,
                "domain": self.smb_domain_entry.get_text().strip(),
                "use_kerberos": "true" if kerberos else "false",
            }
            if kerberos:
                spn = self.smb_spn_entry.get_text().strip()
                if spn:
                    parameters["spn"] = spn
            else:
                password = self.smb_password_entry.get_text()
                if password:
                    parameters["pass"] = password
            return parameters, ""

        if provider == "b2":
            account = self.b2_account_entry.get_text().strip()
            key = self.b2_key_entry.get_text()
            if not self.editing_existing and (not account or not key):
                return {}, tr("remote_b2_credentials_required")
            if self.editing_existing and bool(account) != bool(key):
                return {}, tr("remote_b2_credentials_together")
            parameters = {
                "hard_delete": (
                    "true"
                    if self.b2_hard_delete_row.get_active()
                    else "false"
                ),
            }
            if account:
                parameters["account"] = account
            if key:
                parameters["key"] = key
            return parameters, ""

        if provider == "s3":
            env_auth = self.s3_env_auth_row.get_active()
            access_key = self.s3_access_key_entry.get_text().strip()
            secret_key = self.s3_secret_key_entry.get_text()
            if not env_auth:
                if (
                    not self.editing_existing
                    and (not access_key or not secret_key)
                ):
                    return {}, tr("remote_s3_credentials_required")
                if (
                    self.editing_existing
                    and bool(access_key) != bool(secret_key)
                ):
                    return {}, tr("remote_s3_credentials_together")

            parameters = {
                "provider": self._selected_s3_provider(),
                "env_auth": "true" if env_auth else "false",
                "endpoint": self.s3_endpoint_entry.get_text().strip(),
                "region": self.s3_region_entry.get_text().strip(),
            }
            if not env_auth and access_key:
                parameters["access_key_id"] = access_key
            if not env_auth and secret_key:
                parameters["secret_access_key"] = secret_key
            return parameters, ""

        if provider == "google cloud storage":
            auth = self._selected_id(
                self.google_cloud_auth_combo,
                self.GOOGLE_CLOUD_AUTH_IDS,
            )
            parameters = {
                "project_number": (
                    self.google_cloud_project_entry.get_text().strip()
                ),
            }
            if auth == "oauth":
                parameters.update(
                    {
                        "client_id": (
                            self.google_cloud_client_id_entry
                            .get_text()
                            .strip()
                        ),
                        "client_secret": (
                            self.google_cloud_client_secret_entry
                            .get_text()
                            .strip()
                        ),
                    }
                )
            elif auth == "service_account":
                service_account = (
                    self.google_cloud_service_account_entry
                    .get_text()
                    .strip()
                )
                if not service_account:
                    return {}, tr("remote_service_account_required")
                if not Path(expand_path(service_account)).is_file():
                    return {}, tr(
                        "remote_file_not_found",
                        path=service_account,
                    )
                parameters["service_account_file"] = service_account
            elif auth == "environment":
                parameters["env_auth"] = "true"
            else:
                parameters["anonymous"] = "true"
            return parameters, ""

        if provider in ("azureblob", "azurefiles"):
            return self._collect_azure_parameters(provider)

        if provider == "sftp":
            auth = self._selected_id(self.sftp_auth_combo, self.SFTP_AUTH_IDS)
            host = self.sftp_host_entry.get_text().strip()
            if (
                auth != "external"
                and not host
                and (
                    not self.editing_existing
                    or self.initial_sftp_auth == "external"
                )
            ):
                return {}, tr("remote_host_required")

            parameters: dict[str, str] = {}
            if auth == "external":
                ssh_command = self.sftp_ssh_command_entry.get_text().strip()
                if (
                    not ssh_command
                    and (
                        not self.editing_existing
                        or self.initial_sftp_auth != "external"
                    )
                ):
                    return {}, tr("remote_ssh_command_required")
                if ssh_command:
                    parameters["ssh"] = ssh_command
                return parameters, ""

            port, error = self._validated_port(self.sftp_port_entry.get_text())
            if error:
                return {}, error
            parameters.update(
                {
                    "host": host,
                    "user": self.sftp_user_entry.get_text().strip(),
                    "port": port,
                }
            )
            if auth == "agent":
                parameters["key_use_agent"] = "true"
            elif auth == "password":
                password = self.sftp_password_entry.get_text()
                if not password and not self.editing_existing:
                    return {}, tr("remote_password_required")
                if password:
                    parameters["pass"] = password
            else:
                key_file = self.sftp_key_file_entry.get_text().strip()
                if (
                    not key_file
                    and (
                        not self.editing_existing
                        or self.initial_sftp_auth != "key"
                    )
                ):
                    return {}, tr("remote_key_file_required")
                if key_file and not Path(expand_path(key_file)).is_file():
                    return {}, tr("remote_file_not_found", path=key_file)
                if key_file:
                    parameters["key_file"] = key_file
                key_password = self.sftp_key_pass_entry.get_text()
                if key_password:
                    parameters["key_file_pass"] = key_password

            if self.sftp_verify_host_row.get_active():
                known_hosts = self.sftp_known_hosts_entry.get_text().strip()
                if not known_hosts and not self.editing_existing:
                    return {}, tr("remote_known_hosts_required")
                if (
                    known_hosts
                    and not Path(expand_path(known_hosts)).is_file()
                ):
                    return {}, tr("remote_file_not_found", path=known_hosts)
                if known_hosts:
                    parameters["known_hosts_file"] = known_hosts
            return parameters, ""

        if provider == "ftp":
            host = self.ftp_host_entry.get_text().strip()
            if not host and not self.editing_existing:
                return {}, tr("remote_host_required")
            port, error = self._validated_port(self.ftp_port_entry.get_text())
            if error:
                return {}, error
            parameters = {
                "host": host,
                "user": self.ftp_user_entry.get_text().strip(),
                "port": port,
                "pass": self.ftp_password_entry.get_text(),
            }
            security = self._selected_id(
                self.ftp_security_combo,
                self.FTP_SECURITY_IDS,
            )
            if security == "explicit_tls":
                parameters["explicit_tls"] = "true"
            elif security == "implicit_tls":
                parameters["tls"] = "true"
            if (
                security != "plain"
                and not self.ftp_verify_certificate_row.get_active()
            ):
                parameters["no_check_certificate"] = "true"
            return parameters, ""

        return {}, tr("remote_provider_unsupported")

    def _collect_azure_parameters(
        self,
        provider: str,
    ) -> tuple[dict[str, str], str]:
        fields = self.azure_fields[provider]
        auth_combo = fields["auth"]
        if not isinstance(auth_combo, Adw.ComboRow):
            return {}, tr("remote_provider_unsupported")
        auth = self._selected_id(auth_combo, self.AZURE_AUTH_IDS)
        get_text = lambda key: fields[key].get_text().strip()
        parameters: dict[str, str] = {}

        if auth == "account_key":
            account = get_text("account")
            key = get_text("key")
            if not account:
                return {}, tr("remote_azure_account_required")
            if not key and not self.editing_existing:
                return {}, tr("remote_azure_key_required")
            parameters["account"] = account
            if key:
                parameters["key"] = key
        elif auth == "sas_url":
            sas_url = get_text("sas_url")
            if not sas_url and not self.editing_existing:
                return {}, tr("remote_azure_sas_required")
            if sas_url:
                parameters["sas_url"] = sas_url
        elif auth == "connection_string":
            connection_string = get_text("connection_string")
            if not connection_string and not self.editing_existing:
                return {}, tr("remote_azure_connection_required")
            if connection_string:
                parameters["connection_string"] = connection_string
        elif auth == "environment":
            parameters["env_auth"] = "true"
        else:
            account = get_text("account")
            tenant = get_text("tenant")
            client_id = get_text("client_id")
            client_secret = get_text("client_secret")
            if not account:
                return {}, tr("remote_azure_account_required")
            if not tenant or not client_id:
                return {}, tr("remote_azure_principal_required")
            if not client_secret and not self.editing_existing:
                return {}, tr("remote_password_required")
            parameters.update(
                {
                    "account": account,
                    "tenant": tenant,
                    "client_id": client_id,
                }
            )
            if client_secret:
                parameters["client_secret"] = client_secret

        if provider == "azurefiles":
            parameters["share_name"] = get_text("share_name")
        return parameters, ""

    def _validated_port(self, value: str) -> tuple[str, str]:
        text = value.strip()
        try:
            port = int(text)
        except ValueError:
            return "", tr("remote_port_invalid")
        if not 1 <= port <= 65535:
            return "", tr("remote_port_invalid")
        return str(port), ""

    def _confirm_plain_ftp(self) -> None:
        dialog = Adw.AlertDialog.new(
            tr("remote_ftp_plain_warning_title"),
            tr("remote_ftp_plain_warning_body"),
        )
        dialog.add_response("cancel", tr("cancel"))
        dialog.add_response("continue", tr("continue"))
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance(
            "continue",
            Adw.ResponseAppearance.DESTRUCTIVE,
        )
        dialog.connect("response", self._on_plain_ftp_response)
        dialog.present(self)

    def _on_plain_ftp_response(
        self,
        _dialog: Adw.AlertDialog,
        response: str,
    ) -> None:
        if response != "continue":
            return
        self._ftp_plain_confirmed = True
        self._validate_and_start_creation()

    def _on_config_step(self, value: object) -> None:
        if not isinstance(value, RemoteConfigStep):
            self._show_configuration_failure(str(value))
            return
        if not value.command_ok:
            self._show_configuration_failure(
                value.error or tr("remote_config_unknown_error")
            )
            return
        if value.complete:
            self._start_connection_test()
            return

        option = value.option or {}
        if str(option.get("Name", "")) == "config_is_local":
            self.current_config_state = value.state
            self._show_progress(
                tr("remote_oauth_authorizing"),
                tr("remote_oauth_browser_wait"),
            )
            self._continue_configuration("true")
            return

        self._show_config_question(value)

    def _show_config_question(self, step: RemoteConfigStep) -> None:
        option = step.option or {}
        self.current_config_state = step.state
        self.question_values = []
        self.question_widget = None
        self.question_custom_entry = None

        page = Adw.PreferencesPage()
        help_text = str(option.get("Help", "") or "").strip()
        group = Adw.PreferencesGroup(
            title=tr("remote_config_additional_step"),
            description=_escape_pango_markup(help_text),
        )
        page.add(group)

        error = step.error.strip()
        if error:
            error_label = Gtk.Label(label=error, xalign=0)
            error_label.set_wrap(True)
            error_label.add_css_class("error")
            error_label.set_margin_top(6)
            error_label.set_margin_bottom(6)
            error_label.set_margin_start(12)
            error_label.set_margin_end(12)
            group.add(error_label)

        examples = option.get("Examples")
        option_type = str(option.get("Type", "string"))
        title = self._question_title(option)

        choices = _config_question_choices(examples)
        if choices:
            model = Gtk.StringList()
            for label, value in choices:
                model.append(label)
                self.question_values.append(value)

            allow_custom = not bool(option.get("Exclusive"))
            if allow_custom:
                model.append(tr("remote_question_custom_value"))
                self.question_values.append(None)

            question_box = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL,
                spacing=8,
            )
            question_box.set_margin_top(10)
            question_box.set_margin_bottom(10)
            question_box.set_margin_start(12)
            question_box.set_margin_end(12)

            title_label = Gtk.Label(label=title, xalign=0)
            title_label.add_css_class("heading")
            title_label.set_wrap(True)
            question_box.append(title_label)

            combo = Gtk.DropDown(model=model)
            combo.set_enable_search(True)
            combo.set_hexpand(True)
            combo.set_factory(self._question_dropdown_factory(False))
            combo.set_list_factory(self._question_dropdown_factory(True))
            default = self._config_value_text(option.get("Default"))
            try:
                combo.set_selected(self.question_values.index(default))
            except ValueError:
                if allow_custom:
                    combo.set_selected(len(self.question_values) - 1)
                else:
                    combo.set_selected(0)

            question_box.append(combo)
            if allow_custom:
                custom_entry = Gtk.Entry()
                custom_entry.set_hexpand(True)
                custom_entry.set_placeholder_text(
                    tr("remote_question_custom_value")
                )
                if default not in {
                    value
                    for value in self.question_values
                    if value is not None
                }:
                    custom_entry.set_text(default)
                combo.connect(
                    "notify::selected",
                    self._on_question_choice_changed,
                )
                question_box.append(custom_entry)
                self.question_custom_entry = custom_entry
                self._on_question_choice_changed(combo, object())

            self.question_widget = combo
            group.add(question_box)
        elif option_type == "bool":
            row = Adw.SwitchRow(title=title)
            default = self._config_value_text(option.get("Default"))
            row.set_active(default.casefold() == "true")
            self.question_widget = row
            group.add(row)
        elif bool(option.get("IsPassword")):
            row = self._password_row(title)
            row.set_text(self._config_value_text(option.get("Default")))
            self.question_widget = row
            group.add(row)
        else:
            row = self._entry_row(title)
            row.set_text(self._config_value_text(option.get("Default")))
            self.question_widget = row
            group.add(row)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_child(page)

        old_page = self.page_stack.get_child_by_name("question")
        if old_page is not None:
            self.page_stack.remove(old_page)
        self.page_stack.add_named(scrolled, "question")
        self.page_stack.set_visible_child_name("question")
        self.back_button.set_visible(False)
        self.primary_button.set_label(tr("continue"))
        self.primary_button.set_visible(True)
        self.primary_button.set_sensitive(True)

    def _question_title(self, option: dict[str, Any]) -> str:
        name = str(option.get("Name", "") or "")
        known_titles = {
            "choose_type": tr("remote_question_choose_type"),
            "config_type": tr("remote_question_choose_type"),
            "drive_id": tr("remote_question_choose_drive"),
            "drive_type": tr("remote_question_choose_drive"),
            "config_driveid": tr("remote_question_choose_drive"),
            "config_driveid_fixed": tr("remote_question_drive_id"),
            "team_drive": tr("remote_question_shared_drive"),
            "root_folder_id": tr("remote_question_root_folder"),
        }
        return known_titles.get(
            name,
            name.replace("_", " ").strip().capitalize()
            or tr("remote_question_answer"),
        )

    @staticmethod
    def _question_dropdown_factory(
        wrap: bool,
    ) -> Gtk.SignalListItemFactory:
        factory = Gtk.SignalListItemFactory()

        def setup(
            _factory: Gtk.SignalListItemFactory,
            item: Gtk.ListItem,
        ) -> None:
            label = Gtk.Label(xalign=0)
            label.set_wrap(wrap)
            label.set_max_width_chars(72)
            label.set_margin_top(5 if wrap else 0)
            label.set_margin_bottom(5 if wrap else 0)
            item.set_child(label)

        def bind(
            _factory: Gtk.SignalListItemFactory,
            item: Gtk.ListItem,
        ) -> None:
            value = item.get_item()
            label = item.get_child()
            if not isinstance(value, Gtk.StringObject):
                return
            if not isinstance(label, Gtk.Label):
                return
            text = value.get_string()
            label.set_text(text)
            label.set_tooltip_text(text)

        factory.connect("setup", setup)
        factory.connect("bind", bind)
        return factory

    def _on_question_choice_changed(
        self,
        combo: Gtk.DropDown,
        _pspec: object,
    ) -> None:
        if self.question_custom_entry is None:
            return
        selected = combo.get_selected()
        use_custom = (
            selected >= len(self.question_values)
            or self.question_values[selected] is None
        )
        self.question_custom_entry.set_visible(use_custom)

    def _submit_question_answer(self) -> None:
        widget = self.question_widget
        if isinstance(widget, Gtk.DropDown):
            selected = widget.get_selected()
            if selected >= len(self.question_values):
                self._show_error(tr("remote_question_answer_required"))
                return
            selected_value = self.question_values[selected]
            if selected_value is None:
                answer = (
                    self.question_custom_entry.get_text().strip()
                    if self.question_custom_entry is not None
                    else ""
                )
                if not answer:
                    self._show_error(
                        tr("remote_question_answer_required")
                    )
                    return
            else:
                answer = selected_value
        elif isinstance(widget, Adw.SwitchRow):
            answer = "true" if widget.get_active() else "false"
        elif isinstance(widget, (Adw.EntryRow, Adw.PasswordEntryRow)):
            answer = widget.get_text()
        else:
            self._show_error(tr("remote_question_answer_required"))
            return

        self._show_progress(
            tr("remote_config_processing"),
            tr("remote_config_please_wait"),
        )
        self._continue_configuration(answer)

    def _continue_configuration(self, answer: str) -> None:
        state = self.current_config_state
        self._run_async(
            lambda: continue_remote_config(
                self.remote_name,
                state,
                answer,
                self.base_parameters,
                self.runner,
            ),
            self._on_config_step,
        )

    @staticmethod
    def _config_value_text(value: object) -> str:
        if value is True:
            return "true"
        if value is False:
            return "false"
        if value is None:
            return ""
        if isinstance(value, (list, dict)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    def _start_connection_test(self) -> None:
        self._show_progress(
            tr("remote_test_title"),
            tr("remote_test_help", name=self.remote_name),
        )
        self._run_async(
            lambda: test_remote_connection(
                self.remote_name,
                self.runner,
            ),
            self._on_connection_tested,
        )

    def _on_connection_tested(self, value: object) -> None:
        if not isinstance(value, RcloneCommandResult):
            self._show_test_failure(str(value))
            return
        if value.ok:
            self._finish_success()
            return
        if value.timed_out:
            error = tr("remote_test_timeout")
        else:
            error = value.stderr.strip() or value.stdout.strip()
            if not error:
                error = tr("remote_config_unknown_error")
        self._show_test_failure(error)

    def _show_test_failure(self, error: str) -> None:
        self.result_icon.set_from_icon_name("dialog-warning-symbolic")
        self.result_title.set_text(tr("remote_test_failed"))
        self.result_error.set_text(error)
        self.retry_button.set_visible(True)
        self.use_anyway_button.set_visible(True)
        self.remove_button.set_visible(True)
        if self.editing_existing:
            self.remove_button.set_label(tr("remote_return_to_edit"))
            self.remove_button.remove_css_class("destructive-action")
        else:
            self.remove_button.set_label(tr("remote_remove_and_return"))
            self.remove_button.add_css_class("destructive-action")
        self.page_stack.set_visible_child_name("result")
        self.back_button.set_visible(False)
        self.primary_button.set_visible(False)

    def _show_configuration_failure(self, error: str) -> None:
        self.result_icon.set_from_icon_name("dialog-error-symbolic")
        self.result_title.set_text(tr("remote_config_failed"))
        self.result_error.set_text(error)
        self.retry_button.set_visible(False)
        self.use_anyway_button.set_visible(False)
        self.remove_button.set_visible(True)
        if self.editing_existing:
            self.remove_button.set_label(tr("remote_return_to_edit"))
            self.remove_button.remove_css_class("destructive-action")
        else:
            self.remove_button.set_label(tr("remote_remove_and_return"))
            self.remove_button.add_css_class("destructive-action")
        self.page_stack.set_visible_child_name("result")
        self.back_button.set_visible(False)
        self.primary_button.set_visible(False)

    def _on_remove_and_return_clicked(self, _button: Gtk.Button) -> None:
        if self.editing_existing:
            self._show_wizard_page("setup")
            return
        self._show_progress(
            tr("remote_config_removing"),
            tr("remote_config_please_wait"),
        )
        self._run_async(
            lambda: delete_remote_config(self.remote_name),
            self._on_remote_removed,
        )

    def _on_remote_removed(self, value: object) -> None:
        ok = False
        error = ""
        if isinstance(value, tuple) and len(value) == 2:
            ok, error = bool(value[0]), str(value[1])
        if not ok and remote_name_exists(self.remote_name):
            self._show_configuration_failure(
                error or tr("remote_remove_failed")
            )
            return

        self._config_started = False
        self._cleanup_started = False
        self.remote_name = ""
        self.remote_type = ""
        self.base_parameters = {}
        self.current_config_state = ""
        self._show_wizard_page("provider")

    def _show_progress(self, title: str, body: str) -> None:
        self.progress_title.set_text(title)
        self.progress_body.set_text(body)
        self.progress_spinner.start()
        self.page_stack.set_visible_child_name("progress")
        self.back_button.set_visible(False)
        self.primary_button.set_visible(False)

    def _show_error(self, message: str) -> None:
        dialog = Adw.AlertDialog.new(tr("error"), message)
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self)

    def _run_async(
        self,
        operation: Callable[[], object],
        callback: Callable[[object], None],
    ) -> None:
        self._operation_serial += 1
        serial = self._operation_serial
        self._operation_running = True

        def worker() -> None:
            try:
                result: object = operation()
            except Exception as exc:
                result = exc
            GLib.idle_add(
                self._finish_async,
                serial,
                callback,
                result,
            )

        threading.Thread(target=worker, daemon=True).start()

    def _finish_async(
        self,
        serial: int,
        callback: Callable[[object], None],
        result: object,
    ) -> bool:
        if serial != self._operation_serial:
            return False
        self._operation_running = False
        if self._closed:
            self._cleanup_remote_async()
            return False
        if isinstance(result, Exception):
            self._show_configuration_failure(str(result))
            return False
        callback(result)
        return False

    def _finish_success(self) -> None:
        self._remote_delivered = True
        self._closed = True
        self.on_created(self.remote_name)
        self.force_close()

    def _on_cancel_clicked(self, _button: Gtk.Button) -> None:
        self._cancel_and_close()

    def do_close_attempt(self) -> None:
        self._cancel_and_close()

    def _cancel_and_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.runner.cancel()
        if not self._operation_running:
            self._cleanup_remote_async()
        self.force_close()

    def _cleanup_remote_async(self) -> None:
        if (
            self._cleanup_started
            or not self._config_started
            or self._remote_delivered
            or not self.remote_name
        ):
            return
        self._cleanup_started = True
        remote_name = self.remote_name

        def cleanup() -> None:
            if remote_name_exists(remote_name):
                delete_remote_config(remote_name)

        threading.Thread(target=cleanup, daemon=True).start()


class RemoteEditDialog(RemoteConfigDialog):
    def __init__(
        self,
        parent: Gtk.Window,
        remote_name: str,
        config: dict[str, str],
        on_updated: Callable[[str], None],
        reauthorize: bool = False,
    ) -> None:
        self.existing_remote_name = remote_name
        self.existing_config = dict(config)
        self.reauthorize = reauthorize
        self.initial_sftp_auth = ""
        self.update_clear_keys: set[str] = set()
        super().__init__(parent, on_updated)

        self.editing_existing = True
        self.remote_name = remote_name
        self.remote_name_entry.set_text(remote_name)
        self.remote_name_entry.set_sensitive(False)
        self.provider_combo.set_sensitive(False)
        self.remove_button.set_label(tr("remote_return_to_edit"))
        self.remove_button.remove_css_class("destructive-action")

        remote_type = str(config.get("type", "")).casefold()
        try:
            self.provider_combo.set_selected(
                self.PROVIDER_IDS.index(remote_type)
            )
        except ValueError:
            self.provider_combo.set_selected(0)
        self.remote_type = remote_type
        self._populate_existing_values(remote_type, config)
        self._update_provider_fields()
        self._show_wizard_page("setup")

        if reauthorize:
            self.set_title(
                tr("remote_reauthorize_title", name=remote_name)
            )
            self.primary_button.set_visible(False)
            GLib.idle_add(self._start_reauthorization)
        else:
            self.set_title(tr("remote_edit_title", name=remote_name))
            self.primary_button.set_label(tr("save"))

    @staticmethod
    def _visible_config_value(
        config: dict[str, str],
        key: str,
        default: str = "",
    ) -> str:
        value = str(config.get(key, default))
        return "" if value == "XXX" else value

    @staticmethod
    def _config_enabled(config: dict[str, str], key: str) -> bool:
        return str(config.get(key, "")).casefold() in (
            "1",
            "true",
            "yes",
            "on",
        )

    @staticmethod
    def _set_tuple_combo(
        combo: Adw.ComboRow,
        ids: tuple[str, ...],
        value: str,
    ) -> None:
        try:
            combo.set_selected(ids.index(value))
        except ValueError:
            combo.set_selected(0)

    def _populate_existing_values(
        self,
        remote_type: str,
        config: dict[str, str],
    ) -> None:
        if remote_type == "onedrive":
            self.onedrive_client_id_entry.set_text(
                self._visible_config_value(config, "client_id")
            )
            self._set_tuple_combo(
                self.onedrive_region_combo,
                self.ONEDRIVE_REGION_IDS,
                str(config.get("region", "global")),
            )
            self.onedrive_tenant_entry.set_text(
                self._visible_config_value(config, "tenant")
            )
            return

        if remote_type == "drive":
            self.google_client_id_entry.set_text(
                self._visible_config_value(config, "client_id")
            )
            self._set_tuple_combo(
                self.google_scope_combo,
                self.GOOGLE_SCOPE_IDS,
                str(config.get("scope", "drive")),
            )
            self.google_service_account_entry.set_text(
                self._visible_config_value(config, "service_account_file")
            )
            return

        if remote_type == "dropbox":
            self.dropbox_client_id_entry.set_text(
                self._visible_config_value(config, "client_id")
            )
            return

        if remote_type == "pcloud":
            self.pcloud_client_id_entry.set_text(
                self._visible_config_value(config, "client_id")
            )
            self._set_tuple_combo(
                self.pcloud_region_combo,
                self.PCLOUD_HOST_IDS,
                str(config.get("hostname", "api.pcloud.com")),
            )
            return

        if remote_type == "box":
            self.box_client_id_entry.set_text(
                self._visible_config_value(config, "client_id")
            )
            self._set_tuple_combo(
                self.box_subtype_combo,
                self.BOX_SUBTYPE_IDS,
                str(config.get("box_sub_type", "user")),
            )
            self.box_config_file_entry.set_text(
                self._visible_config_value(config, "box_config_file")
            )
            self.box_root_folder_entry.set_text(
                self._visible_config_value(config, "root_folder_id")
            )
            return

        if remote_type == "protondrive":
            self.proton_user_entry.set_text(
                self._visible_config_value(config, "username")
            )
            self.proton_2fa_entry.set_text(
                self._visible_config_value(config, "2fa")
            )
            return

        if remote_type == "mega":
            self.mega_user_entry.set_text(
                self._visible_config_value(config, "user")
            )
            self.mega_2fa_entry.set_text(
                self._visible_config_value(config, "2fa")
            )
            return

        if remote_type == "iclouddrive":
            self._set_tuple_combo(
                self.icloud_service_combo,
                self.ICLOUD_SERVICE_IDS,
                str(config.get("service", "drive")),
            )
            self.icloud_apple_id_entry.set_text(
                self._visible_config_value(config, "apple_id")
            )
            return

        if remote_type == "seafile":
            self.seafile_url_entry.set_text(
                self._visible_config_value(config, "url")
            )
            self.seafile_user_entry.set_text(
                self._visible_config_value(config, "user")
            )
            self.seafile_2fa_row.set_active(
                self._config_enabled(config, "2fa")
            )
            self.seafile_library_entry.set_text(
                self._visible_config_value(config, "library")
            )
            return

        if remote_type == "webdav":
            self.webdav_url_entry.set_text(
                self._visible_config_value(config, "url")
            )
            vendor = str(config.get("vendor", "other"))
            if vendor in self.WEBDAV_TILE_VENDOR_IDS:
                self.locked_webdav_vendor = vendor
            else:
                self.locked_webdav_vendor = ""
                self._set_tuple_combo(
                    self.webdav_vendor_combo,
                    self.webdav_vendor_ids,
                    vendor,
                )
            self.webdav_user_entry.set_text(
                self._visible_config_value(config, "user")
            )
            if "bearer_token" in config:
                auth = "bearer"
            elif "pass" in config:
                auth = "password"
            else:
                auth = "none"
            self._set_tuple_combo(
                self.webdav_auth_combo,
                self.WEBDAV_AUTH_IDS,
                auth,
            )
            return

        if remote_type == "smb":
            self.smb_host_entry.set_text(
                self._visible_config_value(config, "host")
            )
            self.smb_user_entry.set_text(
                self._visible_config_value(config, "user")
            )
            self.smb_port_entry.set_text(
                self._visible_config_value(config, "port", "445") or "445"
            )
            self.smb_domain_entry.set_text(
                self._visible_config_value(
                    config,
                    "domain",
                    "WORKGROUP",
                )
            )
            self.smb_kerberos_row.set_active(
                self._config_enabled(config, "use_kerberos")
            )
            self.smb_spn_entry.set_text(
                self._visible_config_value(config, "spn")
            )
            return

        if remote_type == "b2":
            self.b2_account_entry.set_text(
                self._visible_config_value(config, "account")
            )
            self.b2_hard_delete_row.set_active(
                self._config_enabled(config, "hard_delete")
            )
            return

        if remote_type == "s3":
            provider = str(config.get("provider", "Other"))
            if provider in self.S3_TILE_PROVIDER_IDS:
                self.locked_s3_provider = provider
            else:
                self.locked_s3_provider = ""
                self._set_tuple_combo(
                    self.s3_provider_combo,
                    self.s3_provider_ids,
                    provider,
                )
            self.s3_env_auth_row.set_active(
                self._config_enabled(config, "env_auth")
            )
            self.s3_access_key_entry.set_text(
                self._visible_config_value(config, "access_key_id")
            )
            self.s3_endpoint_entry.set_text(
                self._visible_config_value(config, "endpoint")
            )
            self.s3_region_entry.set_text(
                self._visible_config_value(config, "region")
            )
            return

        if remote_type == "google cloud storage":
            if self._config_enabled(config, "anonymous"):
                auth = "anonymous"
            elif self._config_enabled(config, "env_auth"):
                auth = "environment"
            elif str(config.get("service_account_file", "")):
                auth = "service_account"
            else:
                auth = "oauth"
            self._set_tuple_combo(
                self.google_cloud_auth_combo,
                self.GOOGLE_CLOUD_AUTH_IDS,
                auth,
            )
            self.google_cloud_client_id_entry.set_text(
                self._visible_config_value(config, "client_id")
            )
            self.google_cloud_project_entry.set_text(
                self._visible_config_value(config, "project_number")
            )
            self.google_cloud_service_account_entry.set_text(
                self._visible_config_value(config, "service_account_file")
            )
            return

        if remote_type in ("azureblob", "azurefiles"):
            self._populate_azure_values(remote_type, config)
            return

        if remote_type == "sftp":
            self.sftp_host_entry.set_text(
                self._visible_config_value(config, "host")
            )
            self.sftp_user_entry.set_text(
                self._visible_config_value(config, "user")
            )
            self.sftp_port_entry.set_text(
                self._visible_config_value(config, "port", "22") or "22"
            )
            key_file = self._visible_config_value(config, "key_file")
            ssh_command = self._visible_config_value(config, "ssh")
            if "ssh" in config:
                auth = "external"
                if ssh_command:
                    self.sftp_ssh_command_entry.set_text(ssh_command)
            elif "key_file" in config:
                auth = "key"
                if key_file:
                    self.sftp_key_file_entry.set_text(key_file)
            elif "pass" in config:
                auth = "password"
            else:
                auth = "agent"
            self.initial_sftp_auth = auth
            self._set_tuple_combo(
                self.sftp_auth_combo,
                self.SFTP_AUTH_IDS,
                auth,
            )
            known_hosts = self._visible_config_value(
                config,
                "known_hosts_file",
            )
            self.sftp_verify_host_row.set_active(
                "known_hosts_file" in config
            )
            if known_hosts:
                self.sftp_known_hosts_entry.set_text(known_hosts)
            return

        if remote_type == "ftp":
            self.ftp_host_entry.set_text(
                self._visible_config_value(config, "host")
            )
            self.ftp_user_entry.set_text(
                self._visible_config_value(config, "user")
            )
            self.ftp_port_entry.set_text(
                self._visible_config_value(config, "port", "21") or "21"
            )
            if self._config_enabled(config, "tls"):
                security = "implicit_tls"
            elif self._config_enabled(config, "explicit_tls"):
                security = "explicit_tls"
            else:
                security = "plain"
            self._set_tuple_combo(
                self.ftp_security_combo,
                self.FTP_SECURITY_IDS,
                security,
            )
            self.ftp_verify_certificate_row.set_active(
                not self._config_enabled(config, "no_check_certificate")
            )

    def _populate_azure_values(
        self,
        provider: str,
        config: dict[str, str],
    ) -> None:
        fields = self.azure_fields[provider]
        auth_combo = fields["auth"]
        if not isinstance(auth_combo, Adw.ComboRow):
            return
        if self._config_enabled(config, "env_auth"):
            auth = "environment"
        elif "connection_string" in config:
            auth = "connection_string"
        elif "sas_url" in config:
            auth = "sas_url"
        elif "tenant" in config or "client_id" in config:
            auth = "service_principal"
        else:
            auth = "account_key"
        self._set_tuple_combo(auth_combo, self.AZURE_AUTH_IDS, auth)
        for key in (
            "account",
            "tenant",
            "client_id",
            "share_name",
        ):
            fields[key].set_text(self._visible_config_value(config, key))

    def _validate_and_start_creation(self) -> None:
        provider = self._selected_id(self.provider_combo, self.PROVIDER_IDS)
        parameters, validation_error = self._collect_parameters(provider)
        if validation_error:
            self._show_error(validation_error)
            return

        if (
            provider == "ftp"
            and self._selected_id(
                self.ftp_security_combo,
                self.FTP_SECURITY_IDS,
            )
            == "plain"
            and not self._ftp_plain_confirmed
        ):
            self._confirm_plain_ftp()
            return

        if provider in self.OAUTH_PROVIDER_IDS:
            parameters["config_refresh_token"] = "false"
        clear_keys: set[str] = set()
        if provider == "box":
            subtype = self._selected_id(
                self.box_subtype_combo,
                self.BOX_SUBTYPE_IDS,
            )
            if subtype == "user":
                clear_keys.add("box_config_file")
            if not self.box_root_folder_entry.get_text().strip():
                clear_keys.add("root_folder_id")
        elif provider == "webdav":
            auth = self._selected_id(
                self.webdav_auth_combo,
                self.WEBDAV_AUTH_IDS,
            )
            clear_keys.update(
                {
                    "password": {"bearer_token"},
                    "bearer": {"pass"},
                    "none": {"pass", "bearer_token"},
                }[auth]
            )
            if auth != "password":
                clear_keys.add("user")
        elif provider == "smb":
            if self.smb_kerberos_row.get_active():
                clear_keys.add("pass")
            else:
                clear_keys.add("spn")
        elif provider == "s3":
            if self.s3_env_auth_row.get_active():
                clear_keys.update({"access_key_id", "secret_access_key"})
            if not self.s3_endpoint_entry.get_text().strip():
                clear_keys.add("endpoint")
            if not self.s3_region_entry.get_text().strip():
                clear_keys.add("region")
        elif provider == "google cloud storage":
            auth = self._selected_id(
                self.google_cloud_auth_combo,
                self.GOOGLE_CLOUD_AUTH_IDS,
            )
            clear_keys.update(
                {
                    "oauth": {
                        "service_account_file",
                        "service_account_credentials",
                        "env_auth",
                        "anonymous",
                    },
                    "service_account": {
                        "client_id",
                        "client_secret",
                        "env_auth",
                        "anonymous",
                    },
                    "environment": {
                        "client_id",
                        "client_secret",
                        "service_account_file",
                        "service_account_credentials",
                        "anonymous",
                    },
                    "anonymous": {
                        "client_id",
                        "client_secret",
                        "service_account_file",
                        "service_account_credentials",
                        "env_auth",
                    },
                }[auth]
            )
        elif provider in ("azureblob", "azurefiles"):
            fields = self.azure_fields[provider]
            auth_combo = fields["auth"]
            if isinstance(auth_combo, Adw.ComboRow):
                auth = self._selected_id(
                    auth_combo,
                    self.AZURE_AUTH_IDS,
                )
                clear_keys.update(
                    {
                        "account_key": {
                            "sas_url",
                            "connection_string",
                            "env_auth",
                            "tenant",
                            "client_id",
                            "client_secret",
                        },
                        "sas_url": {
                            "account",
                            "key",
                            "connection_string",
                            "env_auth",
                            "tenant",
                            "client_id",
                            "client_secret",
                        },
                        "connection_string": {
                            "account",
                            "key",
                            "sas_url",
                            "env_auth",
                            "tenant",
                            "client_id",
                            "client_secret",
                        },
                        "environment": {
                            "account",
                            "key",
                            "sas_url",
                            "connection_string",
                            "tenant",
                            "client_id",
                            "client_secret",
                        },
                        "service_principal": {
                            "key",
                            "sas_url",
                            "connection_string",
                            "env_auth",
                        },
                    }[auth]
                )
        elif provider == "sftp":
            auth = self._selected_id(
                self.sftp_auth_combo,
                self.SFTP_AUTH_IDS,
            )
            if (
                auth == "password"
                and self.initial_sftp_auth != "password"
                and not self.sftp_password_entry.get_text()
            ):
                self._show_error(tr("remote_password_required"))
                return
            clear_keys.update(
                {
                    "agent": {"pass", "key_file", "key_file_pass", "ssh"},
                    "key": {"pass", "key_use_agent", "ssh"},
                    "password": {
                        "key_file",
                        "key_file_pass",
                        "key_use_agent",
                        "ssh",
                    },
                    "external": {
                        "pass",
                        "key_file",
                        "key_file_pass",
                        "key_use_agent",
                        "known_hosts_file",
                    },
                }[auth]
            )
            if not self.sftp_verify_host_row.get_active():
                clear_keys.add("known_hosts_file")
        elif provider == "ftp":
            security = self._selected_id(
                self.ftp_security_combo,
                self.FTP_SECURITY_IDS,
            )
            parameters["tls"] = (
                "true" if security == "implicit_tls" else "false"
            )
            parameters["explicit_tls"] = (
                "true" if security == "explicit_tls" else "false"
            )
            parameters["no_check_certificate"] = (
                "false"
                if self.ftp_verify_certificate_row.get_active()
                else "true"
            )

        self.remote_name = self.existing_remote_name
        self.remote_type = provider
        self.base_parameters = parameters
        self.update_clear_keys = clear_keys
        self._show_progress(
            tr("remote_config_updating"),
            tr("remote_config_please_wait"),
        )
        self._run_async(
            lambda: update_remote_config(
                self.remote_name,
                self.base_parameters,
                self.runner,
                clear_keys=self.update_clear_keys,
            ),
            self._on_config_step,
        )

    def _start_reauthorization(self) -> bool:
        provider = self._selected_id(self.provider_combo, self.PROVIDER_IDS)
        parameters, validation_error = self._collect_parameters(provider)
        if validation_error:
            self._show_configuration_failure(validation_error)
            return False
        parameters["config_refresh_token"] = "true"
        self.remote_name = self.existing_remote_name
        self.remote_type = provider
        self.base_parameters = parameters
        self._show_progress(
            tr("remote_oauth_authorizing"),
            tr("remote_oauth_browser_wait"),
        )
        self._run_async(
            lambda: update_remote_config(
                self.remote_name,
                self.base_parameters,
                self.runner,
            ),
            self._on_config_step,
        )
        return False


class RemoteManagerDialog(Adw.Dialog):
    EDITABLE_TYPES = set(RemoteConfigDialog.PROVIDER_IDS)

    def __init__(
        self,
        parent: Gtk.Window,
        on_changed: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self.parent_window = parent
        self.on_changed = on_changed
        self._refresh_request_id = 0
        self._rendered_running_profile_ids: set[str] = set()

        self.set_title(tr("remote_manager_title"))
        self.set_content_width(820)
        self.set_content_height(650)
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        _hide_header_title_buttons(header)

        add_button = Gtk.Button(
            icon_name="list-add-symbolic",
        )
        add_button.set_tooltip_text(tr("remote_config_add"))
        add_button.connect("clicked", self._on_add_clicked)
        header.pack_start(add_button)

        end_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
        )
        self.refresh_button = Gtk.Button(icon_name="view-refresh-symbolic")
        self.refresh_button.set_tooltip_text(tr("refresh"))
        self.refresh_button.connect("clicked", lambda *_: self.refresh())
        end_actions.append(self.refresh_button)

        close_button = Gtk.Button(icon_name="window-close-symbolic")
        close_button.set_tooltip_text(tr("close"))
        close_button.connect("clicked", lambda *_: self.close())
        end_actions.append(close_button)
        header.pack_end(end_actions)

        toolbar.add_top_bar(header)

        self.scrolled = Gtk.ScrolledWindow()
        self.scrolled.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC,
        )
        self.scrolled.set_vexpand(True)
        toolbar.set_content(self.scrolled)
        self.set_child(toolbar)

    def refresh(self) -> None:
        self._refresh_request_id += 1
        request_id = self._refresh_request_id
        self.refresh_button.set_sensitive(False)

        loading_page = Adw.PreferencesPage()
        loading_group = Adw.PreferencesGroup()
        loading_page.add(loading_group)
        loading = Adw.ActionRow(
            title=tr("remote_config_please_wait"),
        )
        spinner = Gtk.Spinner(spinning=True)
        loading.add_prefix(spinner)
        loading_group.add(loading)
        self.scrolled.set_child(loading_page)

        threading.Thread(
            target=self._refresh_worker,
            args=(request_id,),
            daemon=True,
        ).start()

    def _refresh_worker(self, request_id: int) -> None:
        try:
            remotes = list_remotes()
            profiles = list_profiles()
            running_profiles = {
                profile_id
                for profile_id, profile in profiles.items()
                if self._profile_is_running(profile_id, profile)
            }
            error = ""
        except Exception as exc:
            remotes = []
            profiles = {}
            running_profiles = set()
            error = str(exc)
        GLib.idle_add(
            self._finish_refresh,
            request_id,
            remotes,
            profiles,
            running_profiles,
            error,
        )

    def _finish_refresh(
        self,
        request_id: int,
        remotes: list[dict[str, str]],
        profiles: dict[str, dict[str, Any]],
        running_profiles: set[str],
        error: str,
    ) -> bool:
        if request_id != self._refresh_request_id:
            return False
        self.refresh_button.set_sensitive(True)
        self._rendered_running_profile_ids = running_profiles

        page = Adw.PreferencesPage()
        intro_group = Adw.PreferencesGroup(
            title=tr("remote_manager_connections"),
            description=tr("remote_manager_help"),
        )
        page.add(intro_group)

        if error or not remotes:
            empty_group = Adw.PreferencesGroup()
            page.add(empty_group)
            empty = Gtk.Label(
                label=(
                    f"{tr('remote_manager_read_failed')}\n{error}"
                    if error
                    else tr("remote_manager_empty")
                ),
                xalign=0,
            )
            empty.set_wrap(True)
            empty.add_css_class("dim-label")
            empty.set_margin_top(14)
            empty.set_margin_bottom(14)
            empty.set_margin_start(12)
            empty.set_margin_end(12)
            empty_group.add(empty)
        else:
            for remote in remotes:
                remote_group = Adw.PreferencesGroup()
                remote_group.set_margin_bottom(6)
                remote_group.add(self._remote_row(remote, profiles))
                page.add(remote_group)

        self.scrolled.set_child(page)
        return False

    def _remote_row(
        self,
        remote: dict[str, str],
        profiles: dict[str, dict[str, Any]],
    ) -> Adw.ExpanderRow:
        name = str(remote.get("name", ""))
        remote_type = str(remote.get("type", "") or "unknown")
        used_by = profiles_for_remote(name, profiles)
        subtitle = tr(
            "remote_manager_row_subtitle",
            type=remote_type,
            profiles=len(used_by),
        )
        row = Adw.ExpanderRow(title=name, subtitle=subtitle)
        row.set_subtitle_lines(2)
        row.set_enable_expansion(bool(used_by))
        row.set_expanded(False)
        row.add_prefix(
            _activity_icon(
                icon_for_kind(str(remote.get("kind", remote_type))),
                28,
            )
        )

        if remote_type in RemoteConfigDialog.OAUTH_PROVIDER_IDS:
            reconnect_button = Gtk.Button(icon_name="view-refresh-symbolic")
            reconnect_button.add_css_class("flat")
            reconnect_button.set_valign(Gtk.Align.CENTER)
            reconnect_button.set_tooltip_text(
                tr("remote_manager_reauthorize")
            )
            reconnect_button.connect(
                "clicked",
                lambda _button, remote_name=name: (
                    self._reauthorize_remote(remote_name)
                ),
            )
            row.add_suffix(reconnect_button)

        edit_button = Gtk.Button(icon_name="emblem-system-symbolic")
        edit_button.add_css_class("flat")
        edit_button.set_valign(Gtk.Align.CENTER)
        edit_button.set_tooltip_text(
            (
                tr("remote_manager_edit")
                if remote_type in self.EDITABLE_TYPES
                else tr("remote_manager_edit_unsupported")
            )
        )
        edit_button.set_sensitive(remote_type in self.EDITABLE_TYPES)
        edit_button.connect(
            "clicked",
            lambda _button, remote_name=name: self._edit_remote(remote_name),
        )
        row.add_suffix(edit_button)

        delete_button = Gtk.Button(icon_name="user-trash-symbolic")
        delete_button.add_css_class("flat")
        delete_button.set_valign(Gtk.Align.CENTER)
        delete_button.set_tooltip_text(tr("remote_manager_delete"))
        delete_button.connect(
            "clicked",
            lambda _button, remote_name=name: (
                self._confirm_delete_remote(remote_name)
            ),
        )
        row.add_suffix(delete_button)

        add_profile_button = Gtk.Button(icon_name="list-add-symbolic")
        add_profile_button.add_css_class("flat")
        add_profile_button.set_valign(Gtk.Align.CENTER)
        add_profile_button.set_tooltip_text(tr("add_profile"))
        add_profile_button.connect(
            "clicked",
            lambda _button, remote_name=name: (
                self._add_profile(remote_name)
            ),
        )
        row.add_suffix(add_profile_button)

        for profile_id, profile in sorted(
            used_by.items(),
            key=lambda item: str(
                item[1].get("label", item[0])
            ).casefold(),
        ):
            row.add_row(self._profile_row(profile_id, profile))
        return row

    def _profile_row(
        self,
        profile_id: str,
        profile: dict[str, Any],
    ) -> Adw.ActionRow:
        label = str(profile.get("label", profile_id))
        mount_dir = str(profile.get("mount_dir", ""))
        running = profile_id in self._rendered_running_profile_ids
        subtitle = tr(
            "remote_manager_profile_subtitle",
            profile=profile_id,
            path=mount_dir,
        )
        if running:
            subtitle = f"{subtitle} · {tr('status_mounted')}"

        row = Adw.ActionRow(title=label, subtitle=subtitle)
        row.set_subtitle_lines(2)
        row.add_prefix(
            _activity_icon(
                str(
                    profile.get(
                        "icon",
                        icon_for_kind(str(profile.get("kind", "generic"))),
                    )
                ),
                22,
            )
        )

        actions = Gio.SimpleActionGroup()
        edit_action = Gio.SimpleAction.new("edit", None)
        edit_action.set_enabled(not running)
        edit_action.connect(
            "activate",
            lambda _action, _parameter, selected_profile=profile_id: (
                self._edit_profile(selected_profile)
            ),
        )
        actions.add_action(edit_action)

        delete_action = Gio.SimpleAction.new("delete", None)
        delete_action.connect(
            "activate",
            lambda _action, _parameter, selected_profile=profile_id: (
                self._confirm_delete_profile(selected_profile)
            ),
        )
        actions.add_action(delete_action)

        menu = Gio.Menu()
        menu.append(
            (
                tr("remote_manager_profile_edit_mounted")
                if running
                else tr("remote_manager_profile_edit")
            ),
            "profile.edit",
        )
        menu.append(
            tr("remote_manager_profile_delete"),
            "profile.delete",
        )

        menu_button = Gtk.MenuButton(icon_name="view-more-symbolic")
        menu_button.add_css_class("flat")
        menu_button.set_valign(Gtk.Align.CENTER)
        menu_button.set_tooltip_text(tr("remote_manager_profile_actions"))
        menu_button.insert_action_group("profile", actions)
        menu_button.set_menu_model(menu)
        row.add_suffix(menu_button)
        return row

    @staticmethod
    def _profile_is_running(
        profile_id: str,
        profile: dict[str, Any],
    ) -> bool:
        return is_profile_mounted(profile) or systemd_is_active(profile_id)

    def _on_add_clicked(self, _button: Gtk.Button) -> None:
        RemoteConfigDialog(
            self.parent_window,
            self._on_remote_updated,
        ).present(self)

    def _load_edit_config(
        self,
        remote_name: str,
    ) -> dict[str, str] | None:
        config, error = redacted_remote_config(remote_name)
        if not config:
            self._show_error(
                error or tr("remote_manager_read_failed")
            )
            return None
        remote_type = str(config.get("type", "")).casefold()
        if remote_type not in self.EDITABLE_TYPES:
            self._show_error(tr("remote_manager_edit_unsupported"))
            return None
        return config

    def _edit_remote(self, remote_name: str) -> None:
        config = self._load_edit_config(remote_name)
        if config is None:
            return
        RemoteEditDialog(
            self.parent_window,
            remote_name,
            config,
            self._on_remote_updated,
        ).present(self)

    def _reauthorize_remote(self, remote_name: str) -> None:
        config = self._load_edit_config(remote_name)
        if config is None:
            return
        RemoteEditDialog(
            self.parent_window,
            remote_name,
            config,
            self._on_remote_updated,
            reauthorize=True,
        ).present(self)

    def _edit_profile(self, profile_id: str) -> None:
        profile = list_profiles().get(profile_id)
        if profile is None:
            self.refresh()
            return
        if self._profile_is_running(profile_id, profile):
            self._show_error(tr("remote_manager_profile_edit_mounted"))
            return
        ProfileDialog(
            self.parent_window,
            self._on_profile_updated,
            profile_id=profile_id,
            profile=profile,
        ).present(self)

    def _add_profile(self, remote_name: str) -> None:
        ProfileDialog(
            self.parent_window,
            self._on_profile_updated,
            remote_name=remote_name,
        ).present(self)

    def _confirm_delete_profile(self, profile_id: str) -> None:
        profile = list_profiles().get(profile_id)
        if profile is None:
            self.refresh()
            return
        label = str(profile.get("label", profile_id))
        dialog = Adw.AlertDialog.new(
            tr("remote_profile_delete_title"),
            tr("remote_profile_delete_body", profile=label),
        )
        dialog.add_response("cancel", tr("cancel"))
        dialog.add_response(
            "delete",
            tr("remote_manager_profile_delete"),
        )
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance(
            "delete",
            Adw.ResponseAppearance.DESTRUCTIVE,
        )
        dialog.connect(
            "response",
            self._on_delete_profile_response,
            profile_id,
        )
        dialog.present(self)

    def _on_delete_profile_response(
        self,
        _dialog: Adw.AlertDialog,
        response: str,
        profile_id: str,
    ) -> None:
        if response != "delete":
            return
        profile = list_profiles().get(profile_id)
        if profile is None:
            self._on_profile_updated()
            return
        profiles = {profile_id: profile}
        if not self._stop_profiles_for_removal(profiles):
            return
        clear_mount_folder_icon(profile)
        delete_profiles(profiles)
        self._on_profile_updated()

    def _confirm_delete_remote(self, remote_name: str) -> None:
        profiles = profiles_for_remote(remote_name)

        if profiles:
            body = tr(
                "remote_delete_used_body",
                name=remote_name,
                profiles=", ".join(profiles),
            )
        else:
            body = tr("remote_delete_body", name=remote_name)

        dialog = Adw.AlertDialog.new(
            tr("remote_delete_title"),
            body,
        )
        dialog.add_response("cancel", tr("cancel"))
        dialog.add_response("delete", tr("remote_manager_delete"))
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance(
            "delete",
            Adw.ResponseAppearance.DESTRUCTIVE,
        )
        dialog.connect(
            "response",
            self._on_delete_response,
            remote_name,
        )
        dialog.present(self)

    def _on_delete_response(
        self,
        _dialog: Adw.AlertDialog,
        response: str,
        remote_name: str,
    ) -> None:
        if response != "delete":
            return
        profiles = profiles_for_remote(remote_name)
        if not self._stop_profiles_for_removal(profiles):
            return
        ok, error = delete_remote_config(remote_name)
        if not ok:
            self._show_error(error or tr("remote_delete_failed"))
            return
        for profile in profiles.values():
            clear_mount_folder_icon(profile)
        delete_profiles(profiles)
        self._on_remote_updated(remote_name)

    def _stop_profiles_for_removal(
        self,
        profiles: dict[str, dict[str, Any]],
    ) -> bool:
        # Preflight all running profiles before changing any mount state.
        for profile_id, profile in profiles.items():
            if (self._profile_is_running(profile_id, profile)
                    and probe_transfer_state(profile_id) != "idle"):
                self._show_error(tr("unmount_safety_blocked", profile=profile_id))
                return False
        failed: list[str] = []
        for profile_id, profile in profiles.items():
            if not self._profile_is_running(profile_id, profile):
                continue
            if probe_transfer_state(profile_id) != "idle":
                self._show_error(tr("unmount_safety_blocked", profile=profile_id))
                return False
            set_profile_desired_mounted(profile_id, False)
            set_profile_pending_unmount(profile_id, False)
            systemd_stop(profile_id)
            if is_profile_mounted(profile):
                unmount_raw(profile_id)
            if self._profile_is_running(profile_id, profile):
                failed.append(profile_id)

        if failed:
            self._show_error(
                tr(
                    "remote_profile_unmount_failed",
                    profiles=", ".join(failed),
                )
            )
            return False
        return True

    def _on_remote_updated(self, _remote_name: str) -> None:
        self.refresh()
        if self.on_changed is not None:
            self.on_changed()

    def _on_profile_updated(self) -> None:
        self.refresh()
        if self.on_changed is not None:
            self.on_changed()

    def _show_error(self, message: str) -> None:
        dialog = Adw.AlertDialog.new(tr("error"), message)
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self)


class ProfileDialog(Adw.Dialog):
    def __init__(
        self,
        parent: Adw.ApplicationWindow,
        on_saved: Callable[[], None],
        profile_id: str | None = None,
        profile: dict[str, Any] | None = None,
        remote_name: str | None = None,
    ) -> None:
        super().__init__()

        self.locked_remote_name = (
            str(remote_name or "").strip().rstrip(":")
        )
        title = (
            tr("edit_profile")
            if profile_id
            else (
                tr(
                    "new_profile_for_remote",
                    remote=self.locked_remote_name,
                )
                if self.locked_remote_name
                else tr("new_profile")
            )
        )

        self.set_title(title)
        self.set_content_width(900)
        self.set_content_height(780)

        self.parent_window = parent
        self.on_saved = on_saved
        self.original_profile_id = profile_id
        self.profile = profile or {}
        self.remotes: list[dict[str, str]] = []

        self.remote_ids: list[str] = []
        self.preset_ids: list[str] = []
        self.icon_names = available_icon_names()
        self.icon_buttons: dict[str, Gtk.ToggleButton] = {}
        self.selected_icon_name = ""
        self.icon_manually_selected = False
        self._updating_icon_selection = False
        self.profile_tab_rows: dict[str, Gtk.ListBoxRow] = {}

        self._build_ui()
        self._fill_remotes(self.locked_remote_name or None)
        self._load_initial_values()

        self.remote_combo.connect("notify::selected", self._on_remote_changed)
        self.preset_combo.connect("notify::selected", self._on_preset_changed)

    def _build_ui(self) -> None:
        toolbar = Adw.ToolbarView()

        header = Adw.HeaderBar()
        _hide_header_title_buttons(header)

        cancel_button = Gtk.Button(label=tr("cancel"))
        cancel_button.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel_button)

        self.save_button = Gtk.Button(label=tr("save"))
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", self._on_save_clicked)
        header.pack_end(self.save_button)

        self.profile_tabs = Gtk.Stack()
        self.profile_tabs.set_vexpand(True)
        self.profile_tabs.set_hexpand(True)
        self.profile_tabs.set_transition_type(
            Gtk.StackTransitionType.CROSSFADE
        )

        self.profile_tab_sidebar = Gtk.ListBox()
        self.profile_tab_sidebar.set_selection_mode(
            Gtk.SelectionMode.SINGLE
        )
        self.profile_tab_sidebar.set_vexpand(True)
        self.profile_tab_sidebar.set_size_request(190, -1)
        self.profile_tab_sidebar.add_css_class("navigation-sidebar")
        self.profile_tab_sidebar.connect(
            "row-selected",
            self._on_profile_tab_selected,
        )
        self.profile_tabs.connect(
            "notify::visible-child-name",
            self._sync_profile_tab_selection,
        )

        toolbar.add_top_bar(header)

        tab_layout = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=0,
        )
        tab_layout.append(self.profile_tab_sidebar)

        tab_separator = Gtk.Separator(
            orientation=Gtk.Orientation.VERTICAL
        )
        tab_layout.append(tab_separator)
        tab_layout.append(self.profile_tabs)

        toolbar.set_content(tab_layout)
        self.set_child(toolbar)
        self.set_default_widget(self.save_button)

        self.profile_id_entry = self._entry_row(tr("profile_id"))
        self.label_entry = self._entry_row(tr("label"))

        self.remote_model = Gtk.StringList()
        self.remote_combo = Adw.ComboRow(
            title=tr("remote"),
            model=self.remote_model,
        )
        self.remote_combo.set_enable_search(True)
        add_remote_button = self._suffix_button(
            "list-add-symbolic",
            tr("remote_config_add"),
        )
        add_remote_button.connect("clicked", self._on_add_remote_clicked)
        self.remote_combo.add_suffix(add_remote_button)
        manage_remotes_button = self._suffix_button(
            "application-x-addon-symbolic",
            tr("remote_manager_title"),
        )
        manage_remotes_button.connect(
            "clicked",
            self._on_manage_remotes_clicked,
        )
        self.remote_combo.add_suffix(manage_remotes_button)

        self.remote_path_entry = self._entry_row(tr("remote_path"))
        browse_remote_button = self._suffix_button(
            "folder-open-symbolic",
            tr("browse_remote"),
        )
        browse_remote_button.connect("clicked", self._on_browse_remote_clicked)
        self.remote_path_entry.add_suffix(browse_remote_button)

        self.mount_dir_entry = self._entry_row(tr("mount_dir"))
        browse_button = self._suffix_button(
            "folder-open-symbolic",
            tr("browse"),
        )
        browse_button.connect("clicked", self._on_browse_clicked)
        self.mount_dir_entry.add_suffix(browse_button)

        self.kind_entry = self._entry_row(tr("kind"))
        self.kind_entry.set_sensitive(False)

        self.preset_model = Gtk.StringList()
        self.preset_combo = Gtk.DropDown(
            model=self.preset_model,
        )
        self.preset_combo.set_enable_search(True)
        self.preset_combo.set_valign(Gtk.Align.CENTER)
        self.preset_combo.set_size_request(320, 34)

        for preset_id, preset in PRESETS.items():
            self.preset_ids.append(preset_id)
            self.preset_model.append(tr(preset["label_key"]))

        self._set_combo_active_id(self.preset_combo, self.preset_ids, "balanced")

        self.preset_row = Adw.ActionRow(
            title=tr("preset"),
            subtitle=tr(PRESETS["balanced"]["description_key"]),
        )
        self.preset_row.add_suffix(self.preset_combo)
        self.preset_row.set_activatable_widget(self.preset_combo)

        (
            self.vfs_cache_mode_row,
            self.vfs_cache_mode_combo,
        ) = self._profile_choice_editor(
            tr("vfs_cache_mode"),
            (
                "vfs_cache_mode_off",
                "vfs_cache_mode_minimal",
                "vfs_cache_mode_writes",
                "vfs_cache_mode_full",
            ),
        )

        (
            self.vfs_cache_max_size_row,
            self.vfs_cache_max_size_entry,
        ) = self._profile_value_editor(
            tr("vfs_cache_max_size"),
            DEFAULTS["vfs_cache_max_size"],
            CACHE_SIZE_PRESETS,
            _speed_limit_bytes,
        )
        (
            self.vfs_cache_max_age_row,
            self.vfs_cache_max_age_entry,
        ) = self._profile_value_editor(
            tr("vfs_cache_max_age"),
            DEFAULTS["vfs_cache_max_age"],
            CACHE_AGE_PRESETS,
            _duration_seconds,
        )
        (
            self.dir_cache_time_row,
            self.dir_cache_time_entry,
        ) = self._profile_value_editor(
            tr("dir_cache_time"),
            DEFAULTS["dir_cache_time"],
            DIR_CACHE_TIME_PRESETS,
            _duration_seconds,
        )

        (
            self.umask_row,
            self.umask_combo,
        ) = self._profile_choice_editor(
            tr("umask"),
            (
                "umask_022",
                "umask_002",
                "umask_077",
                "umask_007",
                "umask_000",
                "umask_custom",
            ),
        )
        self.umask_combo.connect(
            "notify::selected",
            self._on_umask_changed,
        )
        self.umask_custom_entry = self._entry_row(
            tr("umask_custom_value")
        )

        general_group = Adw.PreferencesGroup()
        general_rows = [
            self.profile_id_entry,
            self.label_entry,
        ]
        if not self.locked_remote_name:
            general_rows.append(self.remote_combo)
        general_rows.extend(
            [
                self.remote_path_entry,
                self.mount_dir_entry,
                self.kind_entry,
            ]
        )
        for row in general_rows:
            general_group.add(row)

        preset_group = Adw.PreferencesGroup(
            title=tr("preset"),
            description=tr("preset_help"),
        )
        preset_group.add(self.preset_row)

        cache_group = Adw.PreferencesGroup(
            title=tr("cache_settings"),
            description=tr("cache_settings_help"),
        )
        for row in (
            self.vfs_cache_mode_row,
            self.vfs_cache_max_size_row,
            self.vfs_cache_max_age_row,
            self.dir_cache_time_row,
            self.umask_row,
            self.umask_custom_entry,
        ):
            cache_group.add(row)

        custom_group = Adw.PreferencesGroup(
            title=tr("custom_command"),
            description=tr("custom_command_help"),
        )

        self.custom_command_view = Gtk.TextView()
        self.custom_command_view.set_monospace(True)
        self.custom_command_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.custom_command_view.set_vexpand(True)

        custom_scrolled = Gtk.ScrolledWindow()
        custom_scrolled.set_min_content_height(260)
        custom_scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        custom_scrolled.set_vexpand(True)
        custom_scrolled.set_child(self.custom_command_view)
        custom_scrolled.add_css_class("card")

        custom_group.add(custom_scrolled)

        self._add_profile_tab(
            self._build_tab_page(general_group),
            "general",
            tr("profile_tab_general"),
            "document-properties-symbolic",
        )
        self._add_profile_tab(
            self._build_tab_page(preset_group, cache_group),
            "preset",
            tr("profile_tab_preset"),
            "view-list-symbolic",
        )
        self._add_profile_tab(
            self._build_tab_page(self._build_icon_group()),
            "display",
            tr("profile_tab_display"),
            "preferences-desktop-appearance-symbolic",
        )
        self._add_profile_tab(
            self._build_tab_page(custom_group),
            "other",
            tr("profile_tab_other"),
            "utilities-terminal-symbolic",
        )
        self.profile_tab_sidebar.select_row(
            self.profile_tab_rows["general"]
        )

    def _add_profile_tab(
        self,
        child: Gtk.Widget,
        name: str,
        title: str,
        icon_name: str,
    ) -> None:
        page = self.profile_tabs.add_titled(child, name, title)
        page.set_icon_name(icon_name)

        row = Gtk.ListBoxRow()
        row.set_activatable(True)
        content = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
        )
        content.set_margin_top(9)
        content.set_margin_bottom(9)
        content.set_margin_start(10)
        content.set_margin_end(10)

        icon = Gtk.Image(icon_name=icon_name)
        icon.set_pixel_size(18)
        content.append(icon)

        label = Gtk.Label(label=title, xalign=0)
        label.set_hexpand(True)
        content.append(label)

        row.set_child(content)
        self.profile_tab_rows[name] = row
        self.profile_tab_sidebar.append(row)

    def _on_profile_tab_selected(
        self,
        _sidebar: Gtk.ListBox,
        row: Gtk.ListBoxRow | None,
    ) -> None:
        if row is None:
            return
        for name, candidate in self.profile_tab_rows.items():
            if candidate is row:
                self.profile_tabs.set_visible_child_name(name)
                return

    def _sync_profile_tab_selection(
        self,
        _stack: Gtk.Stack,
        _pspec: object,
    ) -> None:
        name = self.profile_tabs.get_visible_child_name()
        row = self.profile_tab_rows.get(name or "")
        if row is not None and self.profile_tab_sidebar.get_selected_row() is not row:
            self.profile_tab_sidebar.select_row(row)

    def _build_tab_page(
        self,
        *groups: Adw.PreferencesGroup,
    ) -> Gtk.ScrolledWindow:
        page = Adw.PreferencesPage()
        for group in groups:
            page.add(group)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_child(page)
        return scrolled

    def _entry_row(self, title: str) -> Adw.EntryRow:
        row = Adw.EntryRow(title=title)
        row.set_activates_default(True)
        return row

    def _suffix_button(self, icon_name: str, tooltip: str) -> Gtk.Button:
        button = Gtk.Button(icon_name=icon_name)
        button.add_css_class("flat")
        button.set_valign(Gtk.Align.CENTER)
        button.set_tooltip_text(tooltip)
        return button

    def _profile_value_editor(
        self,
        title: str,
        value: str,
        presets: tuple[str, ...],
        parser: Callable[[str], float | None],
    ) -> tuple[Adw.ActionRow, Gtk.Entry]:
        row = Adw.ActionRow(title=title)

        editor = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=0,
        )
        editor.add_css_class("linked")
        editor.set_valign(Gtk.Align.CENTER)
        row.add_suffix(editor)

        decrease = Gtk.Button(label="−")
        decrease.set_tooltip_text(tr("decrease_cache_value"))
        decrease.set_size_request(34, 32)
        decrease.set_valign(Gtk.Align.CENTER)
        editor.append(decrease)

        entry = Gtk.Entry()
        entry.set_text(value)
        entry.set_activates_default(True)
        entry.set_width_chars(9)
        entry.set_max_width_chars(14)
        entry.set_size_request(-1, 32)
        entry.set_valign(Gtk.Align.CENTER)
        editor.append(entry)

        increase = Gtk.Button(label="+")
        increase.set_tooltip_text(tr("increase_cache_value"))
        increase.set_size_request(34, 32)
        increase.set_valign(Gtk.Align.CENTER)
        editor.append(increase)

        decrease.connect(
            "clicked",
            self._step_profile_value,
            entry,
            -1,
            presets,
            parser,
        )
        increase.connect(
            "clicked",
            self._step_profile_value,
            entry,
            1,
            presets,
            parser,
        )
        return row, entry

    def _profile_choice_editor(
        self,
        title: str,
        label_keys: tuple[str, ...],
    ) -> tuple[Adw.ActionRow, Gtk.DropDown]:
        model = Gtk.StringList.new(
            [tr(key) for key in label_keys]
        )
        combo = Gtk.DropDown(model=model)
        combo.set_valign(Gtk.Align.CENTER)
        combo.set_size_request(320, 34)

        row = Adw.ActionRow(title=title)
        row.add_suffix(combo)
        row.set_activatable_widget(combo)
        return row, combo

    def _step_profile_value(
        self,
        _button: Gtk.Button,
        entry: Gtk.Entry,
        direction: int,
        presets: tuple[str, ...],
        parser: Callable[[str], float | None],
    ) -> None:
        entry.set_text(
            _stepped_profile_value(
                entry.get_text(),
                direction,
                presets,
                parser,
            )
        )

    def _on_umask_changed(
        self,
        _combo: Gtk.DropDown,
        _pspec: object,
    ) -> None:
        self.umask_custom_entry.set_visible(
            self._selected_umask_id() == "custom"
        )

    def _selected_umask_id(self) -> str:
        selected = self.umask_combo.get_selected()
        return (
            UMASK_IDS[selected]
            if selected < len(UMASK_IDS)
            else DEFAULTS["umask"]
        )

    def _selected_umask(self) -> str:
        selected = self._selected_umask_id()
        if selected == "custom":
            return self.umask_custom_entry.get_text().strip()
        return selected

    def _set_umask(self, value: str) -> None:
        text = value.strip() or DEFAULTS["umask"]
        try:
            selected = UMASK_IDS.index(text)
        except ValueError:
            selected = UMASK_IDS.index("custom")
            self.umask_custom_entry.set_text(text)
        self.umask_combo.set_selected(selected)
        self._on_umask_changed(self.umask_combo, object())

    def _set_vfs_cache_mode(self, value: str) -> None:
        try:
            selected = VFS_CACHE_MODE_IDS.index(value.strip())
        except ValueError:
            selected = VFS_CACHE_MODE_IDS.index(DEFAULTS["vfs_cache_mode"])
        self.vfs_cache_mode_combo.set_selected(selected)

    def _selected_vfs_cache_mode(self) -> str:
        selected = self.vfs_cache_mode_combo.get_selected()
        return (
            VFS_CACHE_MODE_IDS[selected]
            if selected < len(VFS_CACHE_MODE_IDS)
            else DEFAULTS["vfs_cache_mode"]
        )

    def _build_icon_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=tr("folder_icon"),
            description=tr("folder_icon_help"),
        )

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header.set_margin_top(6)
        header.set_margin_bottom(6)
        header.set_margin_start(12)
        header.set_margin_end(12)

        self.icon_status_label = Gtk.Label(xalign=0)
        self.icon_status_label.set_hexpand(True)
        self.icon_status_label.add_css_class("dim-label")
        header.append(self.icon_status_label)

        choose_button = Gtk.Button(label=tr("choose_icon_file"))
        choose_button.connect("clicked", self._on_choose_icon_clicked)
        header.append(choose_button)

        group.add(header)

        self.icon_flow = Gtk.FlowBox()
        self.icon_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.icon_flow.set_min_children_per_line(2)
        self.icon_flow.set_max_children_per_line(6)
        self.icon_flow.set_column_spacing(8)
        self.icon_flow.set_row_spacing(8)
        self.icon_flow.set_margin_top(6)
        self.icon_flow.set_margin_bottom(12)
        self.icon_flow.set_margin_start(12)
        self.icon_flow.set_margin_end(12)

        for icon_name in self.icon_names:
            self._add_icon_choice(icon_name)

        group.add(self.icon_flow)
        return group

    def _add_icon_choice(self, icon_name: str) -> None:
        if icon_name in self.icon_buttons:
            return

        button = Gtk.ToggleButton()
        button.set_tooltip_text(icon_name)
        button.set_size_request(104, 78)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)

        img_path = icon_path(icon_name)
        if img_path:
            icon_file = Gio.File.new_for_path(img_path)
            image = Gtk.Image.new_from_gicon(Gio.FileIcon.new(icon_file))
            image.set_pixel_size(32)
            box.append(image)

        label = Gtk.Label(label=Path(icon_name).stem)
        label.set_max_width_chars(12)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(label)

        button.set_child(box)
        button.connect("toggled", self._on_icon_button_toggled, icon_name)
        self.icon_buttons[icon_name] = button
        self.icon_flow.append(button)

    def _on_icon_button_toggled(
        self,
        button: Gtk.ToggleButton,
        icon_name: str,
    ) -> None:
        if self._updating_icon_selection:
            return

        if button.get_active():
            self._set_selected_icon(icon_name, manual=True)
        elif self.selected_icon_name == icon_name:
            button.set_active(True)

    def _set_selected_icon(self, icon_name: str, manual: bool) -> None:
        if icon_name and icon_name not in self.icon_buttons:
            self.icon_names.append(icon_name)
            self._add_icon_choice(icon_name)

        self.selected_icon_name = icon_name
        if manual:
            self.icon_manually_selected = True

        self._updating_icon_selection = True
        try:
            for current_name, button in self.icon_buttons.items():
                button.set_active(current_name == icon_name)
        finally:
            self._updating_icon_selection = False

        label = icon_name or tr("not_configured")
        self.icon_status_label.set_text(tr("selected_icon", icon=label))

    def _on_choose_icon_clicked(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog.new()
        dialog.set_title(tr("choose_icon_file"))
        dialog.set_modal(True)

        filters = Gio.ListStore.new(Gtk.FileFilter)
        icon_filter = Gtk.FileFilter()
        icon_filter.set_name(tr("supported_images"))
        for pattern in ("*.svg", "*.png", "*.jpg", "*.jpeg", "*.webp"):
            icon_filter.add_pattern(pattern)
        filters.append(icon_filter)
        dialog.set_filters(filters)

        dialog.open(self.parent_window, None, self._on_icon_file_selected)

    def _on_icon_file_selected(
        self,
        dialog: Gtk.FileDialog,
        result: Gio.AsyncResult,
    ) -> None:
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return

        path = file.get_path()
        if not path:
            return

        try:
            icon_name = install_custom_icon(path)
        except Exception:
            self._show_error(tr("unsupported_icon_file"), "display")
            return

        self._set_selected_icon(icon_name, manual=True)

    def _combo_active_id(
        self,
        combo: Adw.ComboRow | Gtk.DropDown,
        ids: list[str],
    ) -> str | None:
        selected = combo.get_selected()
        if selected < len(ids):
            return ids[selected]
        return None

    def _set_combo_active_id(
        self,
        combo: Adw.ComboRow | Gtk.DropDown,
        ids: list[str],
        item_id: str,
    ) -> None:
        try:
            combo.set_selected(ids.index(str(item_id)))
        except ValueError:
            pass

    def _selected_remote_name(self) -> str:
        return self._combo_active_id(self.remote_combo, self.remote_ids) or ""

    def _fill_remotes(
        self,
        preferred_remote: str | None = None,
        preserve_missing: bool = True,
    ) -> None:
        self.remote_ids.clear()
        self.remote_model.splice(0, self.remote_model.get_n_items(), [])
        self.remotes = list_remotes()
        current_remote = preferred_remote or self.profile.get("remote_name")

        for remote in self.remotes:
            remote_id = str(remote["name"])
            self.remote_ids.append(remote_id)
            self.remote_model.append(
                f"{remote['name']} ({remote.get('type') or remote['kind']})"
            )

        if (
            preserve_missing
            and current_remote
            and not any(r["name"] == current_remote for r in self.remotes)
        ):
            self.remote_ids.append(str(current_remote))
            self.remote_model.append(str(current_remote))

        if current_remote and str(current_remote) in self.remote_ids:
            self._set_combo_active_id(
                self.remote_combo,
                self.remote_ids,
                str(current_remote),
            )
        elif self.remote_ids:
            self.remote_combo.set_selected(0)

        self.remote_combo.set_subtitle(
            "" if self.remote_ids else tr("remote_config_empty")
        )

    def _on_add_remote_clicked(self, _button: Gtk.Button) -> None:
        dialog = RemoteConfigDialog(
            self.parent_window,
            self._on_remote_created,
        )
        dialog.present(self)

    def _on_manage_remotes_clicked(self, _button: Gtk.Button) -> None:
        RemoteManagerDialog(
            self.parent_window,
            self._on_remote_manager_changed,
        ).present(self)

    def _on_remote_manager_changed(self) -> None:
        selected_remote = self._selected_remote_name()
        self._fill_remotes(
            selected_remote,
            preserve_missing=False,
        )
        if self.remote_ids:
            self._on_remote_changed(self.remote_combo, object())

    def _on_remote_created(self, remote_name: str) -> None:
        self._fill_remotes(remote_name)
        self._set_combo_active_id(
            self.remote_combo,
            self.remote_ids,
            remote_name,
        )
        self._on_remote_changed(self.remote_combo, object())

    def _load_initial_values(self) -> None:
        if self.original_profile_id:
            self.profile_id_entry.set_text(self.original_profile_id)
            self.profile_id_entry.set_sensitive(False)

            self.label_entry.set_text(
                str(self.profile.get("label", self.original_profile_id))
            )
            self.remote_path_entry.set_text(str(self.profile.get("remote_path", "")))
            self.mount_dir_entry.set_text(str(self.profile.get("mount_dir", "")))
            self.kind_entry.set_text(str(self.profile.get("kind", "generic")))
            icon_name = str(
                self.profile.get(
                    "icon",
                    icon_for_kind(str(self.profile.get("kind", "generic"))),
                )
            )
            self._set_selected_icon(icon_name, manual=False)
            self.icon_manually_selected = True

            self._set_vfs_cache_mode(
                str(self.profile.get("vfs_cache_mode", DEFAULTS["vfs_cache_mode"]))
            )
            self.vfs_cache_max_size_entry.set_text(
                str(
                    self.profile.get(
                        "vfs_cache_max_size",
                        DEFAULTS["vfs_cache_max_size"],
                    )
                )
            )
            self.vfs_cache_max_age_entry.set_text(
                str(
                    self.profile.get(
                        "vfs_cache_max_age",
                        DEFAULTS["vfs_cache_max_age"],
                    )
                )
            )
            self.dir_cache_time_entry.set_text(
                str(self.profile.get("dir_cache_time", DEFAULTS["dir_cache_time"]))
            )
            self._set_umask(
                str(self.profile.get("umask", DEFAULTS["umask"]))
            )

            custom_command = str(self.profile.get("custom_command", "")).strip()
            if not custom_command:
                custom_command = default_custom_command(
                    str(self.profile.get("kind", "generic"))
                )

            self._set_custom_command(custom_command)
            return

        remote_name = self._selected_remote_name() or "remote"
        suggested = self._suggest_profile_id(remote_name)

        self.profile_id_entry.set_text(suggested)
        self.label_entry.set_text(remote_name)
        self.mount_dir_entry.set_text(str(Path.home() / "cloud" / suggested))

        preset_id = "balanced"
        self._set_combo_active_id(self.preset_combo, self.preset_ids, preset_id)
        self._apply_preset_values(preset_id)

    def _suggest_profile_id(self, remote_name: str) -> str:
        base = safe_id(remote_name)
        profiles = list_profiles()

        if base not in profiles:
            return base

        idx = 2
        while f"{base}-{idx}" in profiles:
            idx += 1

        return f"{base}-{idx}"

    def _on_remote_changed(self, _combo: Adw.ComboRow, _pspec: object) -> None:
        if self.original_profile_id:
            self._apply_remote_defaults(force=False)
            return

        remote_name = self._selected_remote_name() or "remote"
        suggested = self._suggest_profile_id(remote_name)

        self.profile_id_entry.set_text(suggested)
        self.label_entry.set_text(remote_name)
        self.mount_dir_entry.set_text(str(Path.home() / "cloud" / suggested))

        self._set_combo_active_id(
            self.preset_combo,
            self.preset_ids,
            "balanced",
        )
        self._apply_preset_values("balanced")

    def _selected_remote_kind(self) -> str:
        remote_name = self._selected_remote_name()

        for remote in self.remotes:
            if remote["name"] == remote_name:
                return remote["kind"]

        return detect_kind(remote_name, "")

    def _apply_remote_defaults(self, force: bool) -> None:
        kind = self._selected_remote_kind()
        self.kind_entry.set_text(kind)
        if not self.selected_icon_name or not self.icon_manually_selected:
            self._set_selected_icon(icon_for_kind(kind), manual=False)

        default_cmd = default_custom_command(kind)
        current_cmd = self._get_custom_command().strip()

        if force or not current_cmd:
            self._set_custom_command(default_cmd)

    def _apply_preset_values(self, preset_id: str) -> None:
        preset = PRESETS[preset_id]
        self.preset_row.set_subtitle(tr(preset["description_key"]))

        self._set_vfs_cache_mode(preset["vfs_cache_mode"])
        self.vfs_cache_max_size_entry.set_text(preset["vfs_cache_max_size"])
        self.vfs_cache_max_age_entry.set_text(preset["vfs_cache_max_age"])
        self.dir_cache_time_entry.set_text(preset["dir_cache_time"])
        self._set_umask(preset["umask"])

        custom_command = preset.get("custom_command")
        if callable(custom_command):
            self._apply_remote_defaults(force=False)
            self._set_custom_command(custom_command())
        else:
            self._apply_remote_defaults(force=True)

    def _apply_current_preset_or_defaults(self) -> None:
        preset_id = self._combo_active_id(self.preset_combo, self.preset_ids)
        if preset_id and callable(PRESETS.get(preset_id, {}).get("custom_command")):
            self._apply_preset_values(preset_id)
        else:
            self._apply_remote_defaults(force=True)

    def _set_custom_command(self, text: str) -> None:
        buf = self.custom_command_view.get_buffer()
        buf.set_text(text)

    def _get_custom_command(self) -> str:
        buf = self.custom_command_view.get_buffer()
        start = buf.get_start_iter()
        end = buf.get_end_iter()
        return buf.get_text(start, end, False)

    def _on_browse_clicked(self, _button: Gtk.Button) -> None:
        current = self.mount_dir_entry.get_text().strip()

        if current:
            target = Path(expand_path(current))
            start_dir = target.parent
        else:
            start_dir = Path.home() / "cloud"

        try:
            start_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            start_dir = Path.home()

        dialog = Gtk.FileDialog.new()
        dialog.set_title(tr("select_folder"))
        dialog.set_modal(True)
        if hasattr(dialog, "set_accept_label"):
            dialog.set_accept_label(tr("select"))
        dialog.set_initial_folder(Gio.File.new_for_path(str(start_dir)))

        dialog.select_folder(
            self.parent_window,
            None,
            self._on_folder_selected,
        )

    def _on_folder_selected(
        self,
        dialog: Gtk.FileDialog,
        result: Gio.AsyncResult,
    ) -> None:
        try:
            file = dialog.select_folder_finish(result)
        except GLib.Error:
            return

        path = file.get_path()
        if path:
            self.mount_dir_entry.set_text(path)

    def _on_browse_remote_clicked(self, _button: Gtk.Button) -> None:
        remote_name = self._selected_remote_name()

        if not remote_name:
            self._show_error(
                tr("missing_field", field=tr("remote")),
                "general",
            )
            return

        initial_path = self.remote_path_entry.get_text().strip()

        def on_selected(path: str) -> None:
            self.remote_path_entry.set_text(path)
            self._apply_current_preset_or_defaults()

        dialog = RemotePathBrowserDialog(
            self.parent_window,
            remote_name,
            initial_path,
            on_selected,
        )
        dialog.present(self.parent_window)

    def _show_error(self, message: str, tab_name: str | None = None) -> None:
        if tab_name:
            self.profile_tabs.set_visible_child_name(tab_name)
        dialog = Adw.AlertDialog.new(tr("error"), message)
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self)

    def _on_save_clicked(self, _button: Gtk.Button) -> None:
        profile_id = self.profile_id_entry.get_text().strip()
        label = self.label_entry.get_text().strip()
        remote_name = self._selected_remote_name()
        remote_path = self.remote_path_entry.get_text().strip()
        mount_dir = self.mount_dir_entry.get_text().strip()

        for field, value in (
            (tr("profile_id"), profile_id),
            (tr("label"), label),
            (tr("remote"), remote_name),
            (tr("mount_dir"), mount_dir),
        ):
            if not value:
                self._show_error(
                    tr("missing_field", field=field),
                    "general",
                )
                return

        profile_id = safe_id(profile_id)

        if not self.original_profile_id and profile_id in list_profiles():
            self._show_error(tr("profile_exists"), "general")
            return

        kind = self._selected_remote_kind()
        remote_spec = build_remote_spec(remote_name, remote_path)
        target_conflict = find_profile_target_conflict(
            self.original_profile_id or profile_id,
            remote_spec,
            mount_dir,
        )
        if target_conflict:
            self._show_error(target_conflict, "general")
            return

        umask = self._selected_umask() or DEFAULTS["umask"]
        if not re.fullmatch(r"[0-7]{3,4}", umask):
            self._show_error(tr("invalid_umask"), "preset")
            return

        profile = {
            "label": label,
            "remote_name": remote_name,
            "remote_path": remote_path,
            "remote_spec": remote_spec,
            "mount_dir": mount_dir,
            "kind": kind,
            "icon": self.selected_icon_name or icon_for_kind(kind),
            "vfs_cache_mode": self._selected_vfs_cache_mode(),
            "vfs_cache_max_size": self.vfs_cache_max_size_entry.get_text().strip()
            or DEFAULTS["vfs_cache_max_size"],
            "vfs_cache_max_age": self.vfs_cache_max_age_entry.get_text().strip()
            or DEFAULTS["vfs_cache_max_age"],
            "dir_cache_time": self.dir_cache_time_entry.get_text().strip()
            or DEFAULTS["dir_cache_time"],
            "umask": umask,
            "custom_command": self._get_custom_command().strip(),
            "hidden": bool(self.profile.get("hidden", False)),
        }

        save_profile(profile_id, profile)
        set_mount_folder_icon(profile)

        self.close()
        self.on_saved()

    def _on_preset_changed(
        self,
        _combo: Gtk.DropDown,
        _pspec: object,
    ) -> None:
        preset_id = self._combo_active_id(self.preset_combo, self.preset_ids)

        if not preset_id or preset_id not in PRESETS:
            return

        self._apply_preset_values(preset_id)


class ProfileCard(Gtk.Frame):
    def __init__(
        self,
        window: "MainWindow",
        profile_id: str,
        profile: dict[str, Any],
        mounted: bool,
        sync_state: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self.window = window
        self.profile_id = profile_id
        self.profile = profile
        self.mounted = mounted
        self.sync_state = sync_state or {}
        self.context_popover: Gtk.Popover | None = None
        self.mounting = (
            not mounted and profile_id in window._mounting_profiles
        )

        self.set_margin_top(3)
        self.set_margin_bottom(3)
        self.set_margin_start(3)
        self.set_margin_end(3)

        if mounted:
            self.set_size_request(205, 118)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            box.set_margin_top(8)
            box.set_margin_bottom(8)
            box.set_margin_start(8)
            box.set_margin_end(8)
        else:
            self.set_hexpand(True)
            self.set_halign(Gtk.Align.FILL)
            self.set_size_request(185, 130)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.set_margin_top(8)
            box.set_margin_bottom(8)
            box.set_margin_start(8)
            box.set_margin_end(8)

        self.card_box = box
        self.set_child(box)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.append(header)

        title = Gtk.Label(label=str(profile.get("label", profile_id)))
        title.set_xalign(0 if mounted else 0.5)
        title.set_hexpand(True)
        title.set_wrap(False)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_max_width_chars(22)
        title.add_css_class("heading")
        header.append(title)

        if mounted:
            has_health_error = bool(
                not self.sync_state.get("connected")
                or self.sync_state.get("cache_out_of_space")
                or self.sync_state.get("cache_error_files")
                or self.sync_state.get("fatal_error")
            )
            if has_health_error:
                status = Gtk.Image(icon_name="dialog-warning-symbolic")
                status.set_tooltip_text(
                    (
                        tr("profile_monitor_unavailable")
                        if not self.sync_state.get("connected")
                        else str(self.sync_state.get("last_error"))
                        or tr("profile_cache_warning")
                    )
                )
            elif self.sync_state.get("is_syncing"):
                status = Gtk.Spinner()
                status.start()
                status.set_tooltip_text(
                    tr(
                        "profile_syncing_tooltip",
                        active=int(self.sync_state.get("active_count", 0)),
                        queued=int(self.sync_state.get("queued_count", 0)),
                    )
                )
            else:
                status = Gtk.Image(icon_name="emblem-ok-symbolic")
                status.set_tooltip_text(tr("profile_synced_tooltip"))
            status.set_size_request(20, 20)
            header.append(status)

        mounted_body: Gtk.Box | None = None
        mounted_icon_column: Gtk.Box | None = None
        mounted_controls: Gtk.Box | None = None
        if mounted:
            mounted_body = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL,
                spacing=9,
            )
            mounted_body.set_hexpand(True)
            box.append(mounted_body)

            mounted_icon_column = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL,
                spacing=4,
            )
            mounted_icon_column.set_halign(Gtk.Align.CENTER)
            mounted_icon_column.set_valign(Gtk.Align.START)
            mounted_body.append(mounted_icon_column)

            mounted_controls = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL,
                spacing=10,
            )
            mounted_controls.set_hexpand(True)
            mounted_controls.set_valign(Gtk.Align.CENTER)

        img_path = remote_icon_path(
            str(profile.get("icon", DEFAULT_REMOTE_ICON_NAME))
        )
        if img_path:
            icon_file = Gio.File.new_for_path(img_path)
            image = Gtk.Image.new_from_gicon(Gio.FileIcon.new(icon_file))
            icon_size = 44 if mounted else 64
            image.set_pixel_size(icon_size)
            image.set_size_request(icon_size, icon_size)
            if mounted_icon_column is not None:
                mounted_icon_column.append(image)
            else:
                box.append(image)
        if mounted_body is not None and mounted_controls is not None:
            mounted_body.append(mounted_controls)

        if mounted:
            quota = self.sync_state.get("quota", {})
            usage = None
            if isinstance(quota, dict) and int(quota.get("total", 0) or 0) > 0:
                total = int(quota.get("total", 0) or 0)
                used = int(quota.get("used", 0) or 0)
                usage = {
                    "fraction": min(max(used / total, 0.0), 1.0),
                    "label": f"{human_size(used)} / {human_size(total)}",
                }
            if usage is None:
                usage = disk_usage_for_path(str(profile.get("mount_dir", "")))
            if usage and mounted_controls is not None:
                usage_details = Gtk.Box(
                    orientation=Gtk.Orientation.VERTICAL,
                    spacing=6,
                )
                usage_details.set_hexpand(True)
                mounted_controls.append(usage_details)

                progress = Gtk.ProgressBar()
                progress.set_fraction(usage["fraction"])
                progress.set_show_text(False)
                progress.set_size_request(120, 8)
                progress.set_hexpand(True)
                usage_details.append(progress)

                usage_label = Gtk.Label(
                    label=usage["label"],
                    xalign=0.5,
                )
                usage_label.set_hexpand(True)
                usage_label.set_ellipsize(Pango.EllipsizeMode.END)
                usage_label.set_tooltip_text(usage["label"])
                usage_label.add_css_class("caption")
                usage_label.add_css_class("dim-label")
                usage_details.append(usage_label)
            if mounted_controls is not None:
                cache_bytes = int(self.sync_state.get("cache_bytes", 0) or 0)
                cache_files = int(self.sync_state.get("cache_files", 0) or 0)
                cache_details = Gtk.Box(
                    orientation=Gtk.Orientation.VERTICAL,
                    spacing=2,
                )
                cache_details.set_hexpand(True)
                mounted_controls.append(cache_details)

                for text in (
                    tr(
                        "profile_cache_size",
                        size=human_size(cache_bytes),
                    ),
                    tr(
                        "profile_cache_files",
                        count=cache_files,
                    ),
                ):
                    cache_label = Gtk.Label(label=text, xalign=0)
                    cache_label.set_hexpand(True)
                    cache_label.set_ellipsize(Pango.EllipsizeMode.END)
                    cache_label.set_tooltip_text(text)
                    cache_label.add_css_class("caption")
                    cache_label.add_css_class("dim-label")
                    cache_details.append(cache_label)

        if not mounted:
            remote = Gtk.Label(
                label=tr("remote_spec_short", spec=str(profile.get("remote_spec", "")))
            )
            remote.set_xalign(0.5)
            remote.set_wrap(False)
            remote.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            remote.set_max_width_chars(28)
            remote.add_css_class("dim-label")
            box.append(remote)

            mount_dir = Gtk.Label(
                label=tr("mount_dir_short", path=str(profile.get("mount_dir", "")))
            )
            mount_dir.set_xalign(0.5)
            mount_dir.set_wrap(False)
            mount_dir.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            mount_dir.set_hexpand(True)
            mount_dir.set_tooltip_text(
                str(profile.get("mount_dir", ""))
            )
            mount_dir.add_css_class("dim-label")
            box.append(mount_dir)

        if mounted:
            button = Gtk.Button(icon_name="media-eject-symbolic")
            button.set_tooltip_text(tr("unmount"))
            button.add_css_class("destructive-action")
        else:
            button = Gtk.Button(
                label=tr("mounting") if self.mounting else tr("mount")
            )
            button.add_css_class("suggested-action")
            button.set_sensitive(not self.mounting)
        button.connect("clicked", self._on_primary_clicked)

        if mounted:
            button.set_size_request(44, 28)
            button.set_halign(Gtk.Align.CENTER)
            if mounted_icon_column is not None:
                mounted_icon_column.append(button)
        else:
            box.append(button)

        gesture = Gtk.GestureClick()
        gesture.set_button(3)
        gesture.connect("released", self._on_right_click)
        self.card_box.add_controller(gesture)

    def _on_primary_clicked(self, _button: Gtk.Button) -> None:
        if self.mounted:
            self.window.request_unmount_profile(self.profile_id)
        else:
            self.window.mount_profile(self.profile_id)

    def _on_right_click(
        self, _gesture: Gtk.GestureClick, _n_press: int, x: float, y: float
    ) -> None:
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1

        if self.context_popover:
            self.context_popover.set_pointing_to(rect)
            self.window.set_context_menu_open(True)
            if not self.context_popover.get_visible():
                self._popup_context_popover_on_idle(self.context_popover)
            return

        popover = Gtk.Popover()
        self.context_popover = popover
        popover.set_parent(self.card_box)
        popover.add_css_class("menu")
        popover.set_has_arrow(True)
        popover.set_autohide(True)
        popover.set_position(Gtk.PositionType.BOTTOM)
        popover.connect("closed", self._on_context_popover_closed)
        self.window.set_context_menu_open(True)

        popover.set_pointing_to(rect)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        box.set_margin_start(4)
        box.set_margin_end(4)

        if self.mounted:
            self._add_popover_button(
                box,
                popover,
                tr("open"),
                "folder-open-symbolic",
                lambda: self.window.open_profile(self.profile_id),
            )
            self._add_popover_button(
                box,
                popover,
                tr("show_log"),
                "text-x-generic-symbolic",
                lambda: self.window.show_log(self.profile_id),
            )
            self._add_popover_button(
                box,
                popover,
                tr("clear_logs"),
                "edit-clear-all-symbolic",
                lambda: self.window.clear_profile_logs(self.profile_id),
            )
            self._add_popover_button(
                box,
                popover,
                tr("properties"),
                "document-properties-symbolic",
                lambda: self.window.show_properties(self.profile_id),
            )
            self._add_popover_button(
                box,
                popover,
                tr("unmount"),
                "media-eject-symbolic",
                lambda: self.window.request_unmount_profile(self.profile_id),
            )
        else:
            self._add_popover_button(
                box,
                popover,
                tr("mounting") if self.mounting else tr("mount"),
                "drive-harddisk-symbolic",
                lambda: self.window.mount_profile(self.profile_id),
                sensitive=not self.mounting,
            )
            self._add_popover_button(
                box,
                popover,
                tr("edit_profile"),
                "document-edit-symbolic",
                lambda: self.window.edit_profile(self.profile_id),
            )
            self._add_popover_button(
                box,
                popover,
                (
                    tr("unhide_profile")
                    if self.profile.get("hidden")
                    else tr("hide_profile")
                ),
                (
                    "view-reveal-symbolic"
                    if self.profile.get("hidden")
                    else "view-conceal-symbolic"
                ),
                lambda: self.window.set_profile_hidden(
                    self.profile_id,
                    not bool(self.profile.get("hidden")),
                ),
            )
            self._add_popover_button(
                box,
                popover,
                tr("show_log"),
                "text-x-generic-symbolic",
                lambda: self.window.show_log(self.profile_id),
            )
            self._add_popover_button(
                box,
                popover,
                tr("clear_logs"),
                "edit-clear-all-symbolic",
                lambda: self.window.clear_profile_logs(self.profile_id),
            )
            self._add_popover_button(
                box,
                popover,
                tr("properties"),
                "document-properties-symbolic",
                lambda: self.window.show_properties(self.profile_id),
            )

        popover.set_child(box)
        self._popup_context_popover_on_idle(popover)

    @staticmethod
    def _popup_context_popover_on_idle(popover: Gtk.Popover) -> None:
        def popup() -> bool:
            if (
                popover.get_parent() is not None
                and not popover.get_visible()
            ):
                popover.popup()
            return False

        GLib.idle_add(popup, priority=GLib.PRIORITY_DEFAULT_IDLE)

    def _on_context_popover_closed(self, popover: Gtk.Popover) -> None:
        self.window.set_context_menu_open(False)

    def _add_popover_button(
        self,
        box: Gtk.Box,
        popover: Gtk.Popover,
        label: str,
        icon_name: str,
        callback: Callable[[], None],
        sensitive: bool = True,
    ) -> None:
        button = Gtk.Button()
        button.add_css_class("flat")
        button.set_has_frame(False)
        button.set_halign(Gtk.Align.FILL)
        button.set_hexpand(True)
        button.set_sensitive(sensitive)
        button.set_size_request(230, 36)

        content = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
        )
        content.set_margin_start(8)
        content.set_margin_end(8)

        icon = Gtk.Image(icon_name=icon_name)
        icon.set_pixel_size(16)
        content.append(icon)

        text = Gtk.Label(label=label, xalign=0)
        text.set_hexpand(True)
        content.append(text)
        button.set_child(content)

        def on_clicked(_button: Gtk.Button) -> None:
            handler_id = 0

            def run_callback(_popover: Gtk.Popover) -> None:
                _popover.disconnect(handler_id)
                callback()

            handler_id = popover.connect("closed", run_callback)
            popover.popdown()

        button.connect("clicked", on_clicked)
        box.append(button)


class PropertiesDialog(Adw.Dialog):
    def __init__(
        self,
        profile_id: str,
        profile: dict[str, Any],
        mount_dir: str,
        mounted: bool,
        current_command: str,
        last_command_log: str,
        usage: dict[str, Any] | None,
    ) -> None:
        super().__init__()

        self.profile_id = profile_id
        self.profile = profile
        self.mount_dir = mount_dir
        self.mounted = mounted
        self.current_command = current_command
        self.last_command_log = last_command_log
        self.usage = usage

        self.set_title(f"{tr('properties')} — {profile_id}")
        self.set_content_width(760)
        self.set_content_height(680)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        _hide_header_title_buttons(header)

        close_button = Gtk.Button(icon_name="window-close-symbolic")
        close_button.set_tooltip_text(tr("close"))
        close_button.connect("clicked", lambda *_: self.close())
        header.pack_end(close_button)
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        page.add(self._build_summary_group())
        page.add(self._build_location_group())
        page.add(self._build_storage_group())
        page.add(self._build_command_group(tr("current_mount_command"), current_command))
        page.add(self._build_command_group(tr("last_started_commands"), last_command_log))

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_child(page)

        toolbar.set_content(scrolled)
        self.set_child(toolbar)

    def _build_summary_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=tr("properties_profile"))
        group.add(self._info_row(tr("status"), self._status_text()))
        group.add(self._info_row(tr("profile_id"), self.profile_id))
        group.add(self._info_row(tr("label"), str(self.profile.get("label", ""))))
        group.add(self._info_row(tr("kind"), str(self.profile.get("kind", ""))))
        return group

    def _build_location_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=tr("properties_locations"))
        group.add(self._info_row(tr("remote"), str(self.profile.get("remote_name", ""))))
        group.add(self._info_row(tr("remote_spec"), str(self.profile.get("remote_spec", ""))))
        group.add(self._info_row(tr("mount_dir"), self.mount_dir))
        group.add(self._info_row(tr("cache_dir"), str(cache_dir_for(self.profile_id))))
        return group

    def _build_storage_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=tr("disk_usage"))

        if not self.usage:
            label = Gtk.Label(label=tr("disk_usage_unavailable"), xalign=0)
            label.set_wrap(True)
            label.set_margin_top(12)
            label.set_margin_bottom(12)
            label.set_margin_start(12)
            label.set_margin_end(12)
            group.add(label)
            return group

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        percent = round(float(self.usage["fraction"]) * 100)
        title = Gtk.Label(label=f"{percent}% {tr('disk_used')}", xalign=0)
        title.add_css_class("title-3")
        box.append(title)

        progress = Gtk.ProgressBar()
        progress.set_fraction(float(self.usage["fraction"]))
        progress.set_show_text(True)
        progress.set_text(str(self.usage["label"]))
        box.append(progress)

        stats = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        stats.append(self._metric_box(tr("disk_used"), str(self.usage["used_label"])))
        stats.append(self._metric_box(tr("disk_free"), str(self.usage["free_label"])))
        stats.append(self._metric_box(tr("disk_total"), str(self.usage["total_label"])))
        box.append(stats)

        group.add(box)
        return group

    def _build_command_group(self, title: str, text: str) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=title)

        view = Gtk.TextView()
        view.set_editable(False)
        view.set_cursor_visible(False)
        view.set_monospace(True)
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        view.get_buffer().set_text(text or "")

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_min_content_height(120)
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(view)
        scrolled.add_css_class("card")

        group.add(scrolled)
        return group

    def _info_row(self, title: str, value: str) -> Adw.ActionRow:
        row = Adw.ActionRow(title=title)
        row.set_subtitle(value or tr("not_configured"))
        row.set_subtitle_lines(3)
        return row

    def _metric_box(self, title: str, value: str) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_hexpand(True)

        title_label = Gtk.Label(label=title, xalign=0)
        title_label.add_css_class("dim-label")
        box.append(title_label)

        value_label = Gtk.Label(label=value, xalign=0)
        value_label.add_css_class("heading")
        value_label.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(value_label)

        return box

    def _status_text(self) -> str:
        return tr("status_mounted") if self.mounted else tr("status_unmounted")


class TextDialog(Adw.Window):
    def __init__(self, title: str, text: str) -> None:
        super().__init__()

        self.set_title(title)
        self.set_default_size(920, 640)
        self.set_resizable(True)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()

        self.search_button = Gtk.ToggleButton(
            icon_name="edit-find-symbolic"
        )
        self.search_button.set_tooltip_text(tr("search_log"))
        self.search_button.connect(
            "toggled",
            self._on_search_toggled,
        )
        header.pack_end(self.search_button)
        toolbar.add_top_bar(header)

        self.search_bar = Gtk.SearchBar()
        self.search_bar.set_key_capture_widget(self)
        self.search_bar.connect(
            "notify::search-mode-enabled",
            self._on_search_mode_changed,
        )

        search_controls = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
        )
        search_controls.set_margin_start(8)
        search_controls.set_margin_end(8)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text(tr("search_log"))
        self.search_entry.set_hexpand(True)
        self.search_entry.connect(
            "search-changed",
            self._on_search_changed,
        )
        self.search_entry.connect(
            "activate",
            lambda *_: self._find_text(backwards=False),
        )
        search_controls.append(self.search_entry)

        previous_button = Gtk.Button(icon_name="go-up-symbolic")
        previous_button.set_tooltip_text(tr("search_previous"))
        previous_button.connect(
            "clicked",
            lambda *_: self._find_text(backwards=True),
        )
        search_controls.append(previous_button)

        next_button = Gtk.Button(icon_name="go-down-symbolic")
        next_button.set_tooltip_text(tr("search_next"))
        next_button.connect(
            "clicked",
            lambda *_: self._find_text(backwards=False),
        )
        search_controls.append(next_button)

        self.search_status = Gtk.Label(label=tr("search_no_results"))
        self.search_status.add_css_class("error")
        self.search_status.set_visible(False)
        search_controls.append(self.search_status)

        self.search_bar.set_child(search_controls)
        toolbar.add_top_bar(self.search_bar)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)

        self.text_view = Gtk.TextView()
        self.text_view.set_editable(False)
        self.text_view.set_cursor_visible(False)
        self.text_view.set_monospace(True)
        self.text_view.set_wrap_mode(Gtk.WrapMode.NONE)
        self.text_view.set_left_margin(12)
        self.text_view.set_right_margin(12)
        self.text_view.set_top_margin(10)
        self.text_view.set_bottom_margin(10)
        self.text_view.get_buffer().set_text(text or "")
        scrolled.set_child(self.text_view)

        toolbar.set_content(scrolled)
        self.set_content(toolbar)

    def _on_search_toggled(
        self,
        button: Gtk.ToggleButton,
    ) -> None:
        self.search_bar.set_search_mode(button.get_active())

    def _on_search_mode_changed(
        self,
        _search_bar: Gtk.SearchBar,
        _pspec: object,
    ) -> None:
        enabled = self.search_bar.get_search_mode()
        if self.search_button.get_active() != enabled:
            self.search_button.set_active(enabled)
        if enabled:
            self.search_entry.grab_focus()

    def _on_search_changed(
        self,
        _entry: Gtk.SearchEntry,
    ) -> None:
        query = self.search_entry.get_text()
        if not query:
            self.search_status.set_visible(False)
            return
        self._find_text(backwards=False, restart=True)

    def _find_text(
        self,
        backwards: bool,
        restart: bool = False,
    ) -> None:
        query = self.search_entry.get_text()
        if not query:
            return

        buffer = self.text_view.get_buffer()
        selection = buffer.get_selection_bounds()
        has_selection = len(selection) == 2
        selection_start = (
            selection[0]
            if has_selection
            else buffer.get_start_iter()
        )
        selection_end = (
            selection[1]
            if has_selection
            else buffer.get_end_iter()
        )
        if restart:
            current = (
                buffer.get_end_iter()
                if backwards
                else buffer.get_start_iter()
            )
        elif has_selection:
            current = selection_start if backwards else selection_end
        else:
            current = (
                buffer.get_end_iter()
                if backwards
                else buffer.get_start_iter()
            )

        flags = Gtk.TextSearchFlags.CASE_INSENSITIVE
        if backwards:
            match = _normalize_text_search_result(
                current.backward_search(
                    query,
                    flags,
                    None,
                )
            )
            if match is None:
                match = _normalize_text_search_result(
                    buffer.get_end_iter().backward_search(
                        query,
                        flags,
                        None,
                    )
                )
        else:
            match = _normalize_text_search_result(
                current.forward_search(
                    query,
                    flags,
                    None,
                )
            )
            if match is None:
                match = _normalize_text_search_result(
                    buffer.get_start_iter().forward_search(
                        query,
                        flags,
                        None,
                    )
                )

        found = match is not None
        self.search_status.set_visible(not found)
        if match is None:
            return
        match_start, match_end = match
        buffer.select_range(match_start, match_end)
        self.text_view.scroll_to_iter(
            match_start,
            0.12,
            False,
            0.0,
            0.0,
        )


class SettingsDialog(Adw.Dialog):
    def __init__(
        self,
        parent: "MainWindow",
        settings: dict[str, Any],
        on_saved: Callable[[dict[str, Any]], None],
    ) -> None:
        super().__init__()

        self.parent_window = parent
        self.settings = dict(settings)
        self.on_saved = on_saved

        self.set_title(tr("settings"))
        self.set_content_width(720)
        self.set_content_height(520)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        _hide_header_title_buttons(header)

        cancel_button = Gtk.Button(label=tr("cancel"))
        cancel_button.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel_button)

        self.save_button = Gtk.Button(label=tr("save"))
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", self._on_save_clicked)
        header.pack_end(self.save_button)
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()

        appearance_group = Adw.PreferencesGroup(
            title=tr("settings_appearance"),
            description=tr("settings_appearance_help"),
        )
        page.add(appearance_group)

        language_model = Gtk.StringList.new(
            [tr(LANGUAGE_LABEL_KEYS[item]) for item in LANGUAGE_IDS]
        )
        self.language_combo = Adw.ComboRow(
            title=tr("settings_language"),
            subtitle=tr("settings_language_help"),
            model=language_model,
        )
        self._set_settings_combo(
            self.language_combo,
            LANGUAGE_IDS,
            str(self.settings.get("language", "system")),
        )
        appearance_group.add(self.language_combo)

        color_scheme_model = Gtk.StringList.new(
            [
                tr("color_scheme_system"),
                tr("color_scheme_light"),
                tr("color_scheme_dark"),
            ]
        )
        self.color_scheme_combo = Adw.ComboRow(
            title=tr("settings_color_scheme"),
            subtitle=tr("settings_color_scheme_help"),
            model=color_scheme_model,
        )
        self._set_settings_combo(
            self.color_scheme_combo,
            COLOR_SCHEME_IDS,
            str(self.settings.get("color_scheme", "system")),
        )
        appearance_group.add(self.color_scheme_combo)

        speed_group = Adw.PreferencesGroup(
            title=tr("settings_speed_limits"),
            description=tr("settings_speed_limits_help"),
        )
        page.add(speed_group)

        speed_limits = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
        )
        speed_limits.set_homogeneous(True)
        speed_limits.set_margin_top(6)
        speed_group.add(speed_limits)

        self.download_limit_entry = self._speed_limit_editor(
            speed_limits,
            tr("download_speed_limit"),
            "go-down-symbolic",
            str(self.settings.get("download_speed_limit", "")),
        )
        self.upload_limit_entry = self._speed_limit_editor(
            speed_limits,
            tr("upload_speed_limit"),
            "go-up-symbolic",
            str(self.settings.get("upload_speed_limit", "")),
        )

        behavior_group = Adw.PreferencesGroup(title=tr("settings_behavior"))
        page.add(behavior_group)

        self.automount_switch = self._switch_row(
            tr("automount_previous"),
            tr("automount_previous_help"),
            bool(self.settings.get("automount_previous")),
        )
        self.auto_remount_switch = self._switch_row(
            tr("auto_remount"),
            tr("auto_remount_help"),
            bool(self.settings.get("auto_remount")),
        )
        self.disable_tray_switch = self._switch_row(
            tr("disable_tray_icon"),
            tr("disable_tray_icon_help"),
            bool(self.settings.get("disable_tray_icon")),
        )
        self.minimize_tray_switch = self._switch_row(
            tr("minimize_to_tray"),
            tr("minimize_to_tray_help"),
            bool(self.settings.get("minimize_to_tray")),
        )
        self.open_after_mount_switch = self._switch_row(
            tr("open_after_mount"),
            tr("open_after_mount_help"),
            bool(self.settings.get("open_after_mount")),
        )
        self.show_hidden_profiles_switch = self._switch_row(
            tr("show_hidden_profiles"),
            tr("show_hidden_profiles_help"),
            bool(self.settings.get("show_hidden_profiles", False)),
        )
        self.notify_complete_switch = self._switch_row(
            tr("notify_transfer_complete"),
            tr("notify_transfer_complete_help"),
            bool(self.settings.get("notify_transfer_complete", False)),
        )
        self.notify_errors_switch = self._switch_row(
            tr("notify_transfer_errors"),
            tr("notify_transfer_errors_help"),
            bool(self.settings.get("notify_transfer_errors", True)),
        )
        self.confirm_close_switch = self._switch_row(
            tr("confirm_close_during_sync"),
            tr("confirm_close_during_sync_help"),
            bool(self.settings.get("confirm_close_during_sync", False)),
        )
        self.inhibit_shutdown_switch = self._switch_row(
            tr("inhibit_shutdown_during_sync"),
            tr("inhibit_shutdown_during_sync_help"),
            bool(
                self.settings.get("inhibit_shutdown_during_sync", False)
            ),
        )

        for row in (
            self.automount_switch,
            self.auto_remount_switch,
            self.open_after_mount_switch,
            self.show_hidden_profiles_switch,
            self.notify_complete_switch,
            self.notify_errors_switch,
            self.confirm_close_switch,
            self.inhibit_shutdown_switch,
            self.disable_tray_switch,
            self.minimize_tray_switch,
        ):
            behavior_group.add(row)

        activity_group = Adw.PreferencesGroup(
            title=tr("activity_hidden_patterns"),
            description=tr("activity_hidden_patterns_help"),
        )
        page.add(activity_group)
        self.activity_patterns_view = Gtk.TextView()
        self.activity_patterns_view.set_monospace(True)
        self.activity_patterns_view.set_wrap_mode(Gtk.WrapMode.NONE)
        self.activity_patterns_view.set_top_margin(10)
        self.activity_patterns_view.set_bottom_margin(10)
        self.activity_patterns_view.set_left_margin(10)
        self.activity_patterns_view.set_right_margin(10)
        self.activity_patterns_view.get_buffer().set_text("\n".join(
            normalize_activity_patterns(self.settings.get("activity_hidden_patterns"))
        ))
        self.activity_patterns_view.update_property(
            [Gtk.AccessibleProperty.LABEL], [tr("activity_hidden_patterns")]
        )
        patterns_scrolled = Gtk.ScrolledWindow()
        patterns_scrolled.set_min_content_height(150)
        patterns_scrolled.set_child(self.activity_patterns_view)
        patterns_scrolled.add_css_class("card")
        activity_group.add(patterns_scrolled)

        context_group = Adw.PreferencesGroup(title=tr("settings_context_menu"))
        page.add(context_group)

        self.context_open_terminal_switch = self._switch_row(
            tr("context_open_external_terminal"),
            tr("context_open_external_terminal_help"),
            bool(self.settings.get("context_open_external_terminal", True)),
        )
        self.context_copy_remote_path_switch = self._switch_row(
            tr("context_copy_remote_path"),
            tr("context_copy_remote_path_help"),
            bool(self.settings.get("context_copy_remote_path", True)),
        )
        self.context_google_new_docs_switch = self._switch_row(
            tr("context_google_new_docs"),
            tr("context_google_new_docs_help"),
            bool(self.settings.get("context_google_new_docs", True)),
        )
        self.context_file_comparison_switch = self._switch_row(
            tr("context_file_comparison"),
            tr("context_file_comparison_help"),
            bool(self.settings.get("context_file_comparison", False)),
        )

        for row in (
            self.context_open_terminal_switch,
            self.context_copy_remote_path_switch,
            self.context_google_new_docs_switch,
        ):
            context_group.add(row)

        experimental_group = Adw.PreferencesGroup(title=tr("settings_experimental"))
        self.context_file_comparison_switch.set_subtitle_lines(0)
        experimental_group.add(self.context_file_comparison_switch)
        page.add(experimental_group)

        integration_group = Adw.PreferencesGroup(
            title=tr("settings_integrations")
        )
        page.add(integration_group)

        self.terminal_entry = Adw.EntryRow(
            title=tr("terminal_command"),
        )
        self.terminal_entry.set_text(
            str(self.settings.get("terminal_command", ""))
        )
        self.terminal_entry.set_activates_default(True)
        integration_group.add(self.terminal_entry)

        terminal_help = Gtk.Label(
            label=tr("terminal_command_help"),
            xalign=0,
        )
        terminal_help.set_wrap(True)
        terminal_help.add_css_class("dim-label")
        terminal_help.set_margin_top(6)
        terminal_help.set_margin_bottom(10)
        terminal_help.set_margin_start(12)
        terminal_help.set_margin_end(12)
        integration_group.add(terminal_help)

        history_group = Adw.PreferencesGroup(
            title=tr("settings_activity_data")
        )
        page.add(history_group)

        history_row = Adw.ActionRow(
            title=tr("activity_history"),
            subtitle=tr("clear_activity_history_help"),
        )
        history_row.set_subtitle_lines(2)
        clear_history_button = Gtk.Button(
            label=tr("clear_activity_history")
        )
        clear_history_button.set_valign(Gtk.Align.CENTER)
        clear_history_button.add_css_class("destructive-action")
        clear_history_button.connect(
            "clicked",
            self._on_clear_history_clicked,
        )
        history_row.add_suffix(clear_history_button)
        history_group.add(history_row)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_child(page)

        toolbar.set_content(scrolled)
        self.set_child(toolbar)
        self.set_default_widget(self.save_button)

    def _switch_row(self, title: str, subtitle: str, active: bool) -> Adw.SwitchRow:
        row = Adw.SwitchRow(title=title, subtitle=subtitle)
        row.set_subtitle_lines(2)
        row.set_active(active)
        return row

    @staticmethod
    def _set_settings_combo(
        combo: Adw.ComboRow,
        ids: tuple[str, ...],
        selected_id: str,
    ) -> None:
        try:
            combo.set_selected(ids.index(selected_id))
        except ValueError:
            combo.set_selected(0)

    @staticmethod
    def _settings_combo_id(
        combo: Adw.ComboRow,
        ids: tuple[str, ...],
    ) -> str:
        selected = combo.get_selected()
        return ids[selected] if selected < len(ids) else ids[0]

    def _speed_limit_editor(
        self,
        parent: Gtk.Box,
        title: str,
        icon_name: str,
        value: str,
    ) -> Gtk.Entry:
        frame = Gtk.Frame()
        frame.set_hexpand(True)
        parent.append(frame)

        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
        )
        content.set_margin_top(10)
        content.set_margin_bottom(10)
        content.set_margin_start(10)
        content.set_margin_end(10)
        frame.set_child(content)

        title_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=7,
        )
        content.append(title_row)

        icon = Gtk.Image(icon_name=icon_name)
        icon.set_pixel_size(16)
        title_row.append(icon)

        title_label = Gtk.Label(label=title, xalign=0)
        title_label.set_wrap(True)
        title_label.set_hexpand(True)
        title_label.add_css_class("caption")
        title_label.add_css_class("dim-label")
        title_row.append(title_label)

        editor = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=0,
        )
        editor.add_css_class("linked")
        content.append(editor)

        decrease = Gtk.Button(label="−")
        decrease.set_tooltip_text(tr("decrease_speed_limit"))
        decrease.set_size_request(36, -1)
        editor.append(decrease)

        entry = Gtk.Entry()
        entry.set_text(value)
        entry.set_placeholder_text(tr("speed_limit_unlimited"))
        entry.set_activates_default(True)
        entry.set_hexpand(True)
        entry.set_width_chars(8)
        entry.set_max_width_chars(12)
        editor.append(entry)

        increase = Gtk.Button(label="+")
        increase.set_tooltip_text(tr("increase_speed_limit"))
        increase.set_size_request(36, -1)
        editor.append(increase)

        decrease.connect(
            "clicked",
            self._step_speed_limit,
            entry,
            -1,
        )
        increase.connect(
            "clicked",
            self._step_speed_limit,
            entry,
            1,
        )
        return entry

    def _step_speed_limit(
        self,
        _button: Gtk.Button,
        entry: Gtk.Entry,
        direction: int,
    ) -> None:
        entry.set_text(
            _stepped_speed_limit(entry.get_text(), direction)
        )

    def _on_save_clicked(self, _button: Gtk.Button) -> None:
        settings = dict(self.settings)
        upload_limit = self.upload_limit_entry.get_text().strip()
        download_limit = self.download_limit_entry.get_text().strip()
        for value in (upload_limit, download_limit):
            if value and not SPEED_LIMIT_RE.fullmatch(value):
                self._show_error(tr("invalid_speed_limit"))
                return

        settings["terminal_command"] = (
            self.terminal_entry.get_text().strip()
            or "gnome-terminal -- bash -lc {ssh_command}"
        )
        settings["automount_previous"] = self.automount_switch.get_active()
        settings["auto_remount"] = self.auto_remount_switch.get_active()
        settings["disable_tray_icon"] = self.disable_tray_switch.get_active()
        settings["minimize_to_tray"] = self.minimize_tray_switch.get_active()
        settings["open_after_mount"] = self.open_after_mount_switch.get_active()
        settings["show_hidden_profiles"] = (
            self.show_hidden_profiles_switch.get_active()
        )
        settings["notify_transfer_complete"] = (
            self.notify_complete_switch.get_active()
        )
        settings["notify_transfer_errors"] = self.notify_errors_switch.get_active()
        settings["confirm_close_during_sync"] = (
            self.confirm_close_switch.get_active()
        )
        settings["inhibit_shutdown_during_sync"] = (
            self.inhibit_shutdown_switch.get_active()
        )
        settings["upload_speed_limit"] = upload_limit
        settings["download_speed_limit"] = download_limit
        settings["language"] = self._settings_combo_id(
            self.language_combo,
            LANGUAGE_IDS,
        )
        settings["color_scheme"] = self._settings_combo_id(
            self.color_scheme_combo,
            COLOR_SCHEME_IDS,
        )
        settings["context_open_external_terminal"] = (
            self.context_open_terminal_switch.get_active()
        )
        settings["context_copy_remote_path"] = (
            self.context_copy_remote_path_switch.get_active()
        )
        settings["context_google_new_docs"] = (
            self.context_google_new_docs_switch.get_active()
        )
        settings["context_file_comparison"] = (
            self.context_file_comparison_switch.get_active()
        )

        patterns_buffer = self.activity_patterns_view.get_buffer()
        settings["activity_hidden_patterns"] = normalize_activity_patterns(
            patterns_buffer.get_text(
                patterns_buffer.get_start_iter(), patterns_buffer.get_end_iter(), True
            )
        )
        save_settings(settings)
        self.on_saved(settings)
        self.close()

    def _show_error(self, message: str) -> None:
        dialog = Adw.AlertDialog.new(tr("error"), message)
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self)

    def _on_clear_history_clicked(self, _button: Gtk.Button) -> None:
        dialog = Adw.AlertDialog.new(
            tr("clear_activity_history_title"),
            tr("clear_activity_history_confirmation"),
        )
        dialog.add_response("cancel", tr("cancel"))
        dialog.add_response("clear", tr("clear_activity_history"))
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance(
            "clear",
            Adw.ResponseAppearance.DESTRUCTIVE,
        )
        dialog.connect("response", self._on_clear_history_response)
        dialog.present(self)

    def _on_clear_history_response(
        self,
        _dialog: Adw.AlertDialog,
        response: str,
    ) -> None:
        if response != "clear":
            return
        self.parent_window.clear_activity_history()
        dialog = Adw.AlertDialog.new(
            tr("info"),
            tr("activity_history_cleared"),
        )
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self)


class ActivityRow(Gtk.ListBoxRow):
    def __init__(
        self,
        window: "MainWindow",
        item: dict[str, Any],
    ) -> None:
        super().__init__()
        self.window = window
        self.item = item
        self._update_signature: tuple[Any, ...] | None = None
        self._delete_styled = False
        self.activity_key = str(
            item.get("event_id")
            or (
                f"{item.get('profile_id', '')}:{item.get('operation', '')}:"
                f"{item.get('path', '')}:{item.get('timestamp', 0)}"
            )
        )
        self.context_popover: Gtk.Popover | None = None
        self.set_activatable(False)
        self.set_selectable(False)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        content.set_margin_top(9)
        content.set_margin_bottom(9)
        content.set_margin_start(10)
        content.set_margin_end(10)
        self.set_child(content)

        thumbnail = Gtk.Image()
        thumbnail.set_pixel_size(56)
        thumbnail.set_size_request(64, 64)
        thumbnail.add_css_class("frame")
        content.append(thumbnail)
        window.load_activity_thumbnail(thumbnail, item)

        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        details.set_hexpand(True)
        content.append(details)

        title_line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        details.append(title_line)

        operation_icon = Gtk.Image(
            icon_name={
                "copy": "view-refresh-symbolic",
                "modify": "document-edit-symbolic",
                "create": "document-new-symbolic",
                "rename": "document-edit-symbolic",
                "delete": "user-trash-symbolic",
            }.get(str(item.get("operation", "copy")), "document-send-symbolic")
        )
        operation_icon.set_pixel_size(16)
        title_line.append(operation_icon)

        title = Gtk.Label(
            label=str(item.get("name") or item.get("path", "")),
            xalign=0,
        )
        title.set_hexpand(True)
        title.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        title.add_css_class("heading")
        self.title_label = title
        title_line.append(title)

        status = Gtk.Label(label=self._status_text(item), xalign=1)
        status.add_css_class("dim-label")
        status.add_css_class("caption")
        self.status_label = status
        title_line.append(status)

        local_path = str(item.get("local_path", ""))
        if local_path:
            details.append(
                self._location_row(
                    activity_display_path(item),
                    str(item.get("profile_icon", DEFAULT_REMOTE_ICON_NAME)),
                    local_path,
                )
            )

        self.size_label = Gtk.Label(xalign=0)
        self.size_label.set_margin_start(25)
        self.size_label.add_css_class("caption")
        self.size_label.add_css_class("dim-label")
        details.append(self.size_label)

        self.error_label = Gtk.Label(xalign=0)
        self.error_label.set_wrap(True)
        self.error_label.add_css_class("error")
        self.error_label.add_css_class("caption")
        details.append(self.error_label)
        self.update_item(item)

        gesture = Gtk.GestureClick()
        gesture.set_button(3)
        gesture.connect("released", self._on_right_click)
        self.add_controller(gesture)

    def _location_row(
        self,
        path: str,
        icon_name: str,
        tooltip_path: str | None = None,
    ) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)

        row.append(_activity_icon(icon_name, 18))

        label = Gtk.Label(label=path, xalign=0)
        label.set_hexpand(True)
        label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        label.set_tooltip_text(tooltip_path or path)
        label.add_css_class("caption")
        row.append(label)
        return row

    def _status_text(self, item: dict[str, Any]) -> str:
        state = str(item.get("state", "completed"))
        operation = str(item.get("operation", "copy"))
        if state == "uploading":
            text = tr("sync_copy_uploading")
        elif state == "retrying":
            text = tr("sync_retrying")
        elif state == "error":
            text = tr("sync_failed")
        elif state == "queued":
            text = tr("sync_copy_queued")
        elif operation == "rename":
            text = tr("sync_renamed")
        elif operation == "delete":
            text = tr("sync_deleted")
        elif operation == "modify":
            text = tr("sync_modified")
        elif operation == "create":
            text = tr("sync_google_created")
        else:
            text = tr("sync_copied")

        extras: list[str] = []
        if "progress" in item:
            extras.append(f"{float(item['progress']):.0f}%")
        speed = float(item.get("speed", 0) or 0)
        if speed > 0:
            extras.append(f"{human_size(speed)}/s")
        tries = int(item.get("tries", 0) or 0)
        if tries > 0:
            extras.append(tr("sync_attempts", count=tries))
        eta = item.get("eta")
        if isinstance(eta, (int, float)) and eta >= 0:
            extras.append(tr("sync_eta", value=self._duration(float(eta))))
        elif state == "queued":
            expiry = float(item.get("expiry", 0) or 0)
            if expiry > 1:
                extras.append(tr("sync_starts_in", value=self._duration(expiry)))
        timestamp = self.window.activity_time(
            float(item.get("timestamp", 0) or 0)
        )
        if timestamp and state in ("completed", "error"):
            extras.append(timestamp)
        if extras:
            return f"{text} · {' · '.join(extras)}"
        return text

    def _duration(self, seconds: float) -> str:
        seconds = max(0, int(seconds))
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}h {minutes}m"
        if minutes:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"

    def _size_text(self, item: dict[str, Any]) -> str:
        size: int | None = None
        try:
            item_size = int(item.get("size", 0) or 0)
        except (TypeError, ValueError):
            item_size = 0
        if item_size > 0 or item.get("size_known"):
            size = max(0, item_size)

        return tr(
            "activity_file_size",
            size=human_size(float(size)) if size is not None else "—",
        )

    @staticmethod
    def _item_update_signature(item: dict[str, Any]) -> tuple[Any, ...]:
        state = str(item.get("state", "completed"))
        progress = item.get("progress")
        if isinstance(progress, (int, float)):
            progress = round(float(progress))
        eta = item.get("eta")
        if isinstance(eta, (int, float)):
            eta = int(eta)
        expiry = item.get("expiry")
        if isinstance(expiry, (int, float)):
            expiry = int(expiry // 5) if state == "queued" else int(expiry)
        return (
            state,
            item.get("operation"),
            item.get("name"),
            item.get("path"),
            item.get("size"),
            item.get("size_known"),
            progress,
            item.get("speed"),
            item.get("tries"),
            eta,
            expiry,
            (
                item.get("timestamp")
                if state in ("completed", "error")
                else None
            ),
            item.get("error"),
        )

    def update_item(self, item: dict[str, Any]) -> bool:
        self.item = item
        signature = self._item_update_signature(item)
        if signature == self._update_signature:
            return False
        self._update_signature = signature

        status = self._status_text(item)
        if self.status_label.get_text() != status:
            self.status_label.set_text(status)
        title = str(item.get("name") or item.get("path", ""))
        if self.title_label.get_text() != title:
            self.title_label.set_text(title)
        size = self._size_text(item)
        if self.size_label.get_text() != size:
            self.size_label.set_text(size)
        deleted = item.get("operation") == "delete"
        if deleted != self._delete_styled:
            attributes = Pango.AttrList()
            if deleted:
                attributes.insert(Pango.attr_strikethrough_new(True))
            self.title_label.set_attributes(attributes)
            self._delete_styled = deleted
        error = str(item.get("error", "") or "")
        if self.error_label.get_text() != error:
            self.error_label.set_text(error)
        self.error_label.set_visible(bool(error))
        return True

    def _on_right_click(
        self,
        _gesture: Gtk.GestureClick,
        _n_press: int,
        x: float,
        y: float,
    ) -> None:
        show_location = self.window.activity_location_available(self.item)
        show_priority = (
            self.item.get("state") in ("queued", "retrying")
            and self.item.get("queue_id") is not None
        )
        if not show_location and not show_priority:
            return

        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1

        if self.context_popover:
            self.context_popover.set_pointing_to(rect)
            self.window.set_context_menu_open(True)
            if not self.context_popover.get_visible():
                self._popup_context_popover_on_idle(self.context_popover)
            return

        popover = Gtk.Popover()
        self.context_popover = popover
        popover.set_parent(self)
        popover.set_has_arrow(True)
        popover.set_autohide(True)
        popover.set_position(Gtk.PositionType.BOTTOM)
        popover.connect("closed", self._on_popover_closed)
        self.window.set_context_menu_open(True)

        popover.set_pointing_to(rect)

        menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        menu.set_margin_top(6)
        menu.set_margin_bottom(6)
        menu.set_margin_start(6)
        menu.set_margin_end(6)
        if show_location:
            location_button = Gtk.Button(label=tr("show_file_location"))
            location_button.connect("clicked", self._show_location)
            menu.append(location_button)
        if show_priority:
            priority_button = Gtk.Button(label=tr("upload_now"))
            priority_button.connect("clicked", self._upload_now)
            menu.append(priority_button)
        popover.set_child(menu)
        self._popup_context_popover_on_idle(popover)

    @staticmethod
    def _popup_context_popover_on_idle(popover: Gtk.Popover) -> None:
        def popup() -> bool:
            if (
                popover.get_parent() is not None
                and not popover.get_visible()
            ):
                popover.popup()
            return False

        GLib.idle_add(popup, priority=GLib.PRIORITY_DEFAULT_IDLE)

    def _show_location(self, _button: Gtk.Button) -> None:
        self._after_popover_closed(
            lambda: self.window.show_activity_location(self.item)
        )

    def _upload_now(self, _button: Gtk.Button) -> None:
        self._after_popover_closed(
            lambda: self.window.prioritize_activity(self.item)
        )

    def _after_popover_closed(
        self,
        callback: Callable[[], None],
    ) -> None:
        popover = self.context_popover
        if popover is None:
            callback()
            return
        handler_id = 0

        def run_callback(_popover: Gtk.Popover) -> None:
            _popover.disconnect(handler_id)
            callback()

        handler_id = popover.connect("closed", run_callback)
        popover.popdown()

    def _on_popover_closed(self, popover: Gtk.Popover) -> None:
        self.window.set_context_menu_open(False)


class ActivityGroupRow(Gtk.ListBoxRow):
    def __init__(
        self,
        window: "MainWindow",
        group: dict[str, Any],
    ) -> None:
        super().__init__()
        self.window = window
        self.group_key = str(group["group_key"])
        self.activity_key = self.group_key
        self._group = group
        self.set_activatable(False)
        self.set_selectable(False)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_child(content)

        self.toggle = Gtk.ToggleButton()
        self.toggle.add_css_class("flat")
        self.toggle.set_hexpand(True)
        content.append(self.toggle)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        header.set_margin_top(9)
        header.set_margin_bottom(9)
        header.set_margin_start(8)
        header.set_margin_end(8)
        self.toggle.set_child(header)

        self.arrow = Gtk.Image(icon_name="pan-end-symbolic")
        self.arrow.set_pixel_size(14)
        header.append(self.arrow)

        header.append(
            _activity_icon(
                str(group.get("profile_icon", DEFAULT_REMOTE_ICON_NAME)),
                28,
            )
        )

        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        labels.set_hexpand(True)
        header.append(labels)

        self.folder_label = Gtk.Label(xalign=0)
        self.folder_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.folder_label.add_css_class("heading")
        labels.append(self.folder_label)

        self.summary_label = Gtk.Label(xalign=0)
        self.summary_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.summary_label.add_css_class("dim-label")
        self.summary_label.add_css_class("caption")
        labels.append(self.summary_label)

        self.progress = Gtk.ProgressBar()
        self.progress.set_valign(Gtk.Align.CENTER)
        self.progress.set_size_request(105, -1)
        self.progress.set_show_text(True)
        header.append(self.progress)
        self._progress_floor = 0.0
        self._progress_paths: frozenset[str] = frozenset()
        self._child_render_generation = 0
        self._child_render_cursor = 0
        self._child_render_keys: list[str] = []
        self._child_render_items: dict[str, dict[str, Any]] = {}
        self._update_signature: tuple[Any, ...] | None = None

        separator = Gtk.Separator(
            orientation=Gtk.Orientation.HORIZONTAL
        )
        content.append(separator)

        self.children_list = Gtk.ListBox()
        self.children_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.children_list.set_show_separators(True)
        self.children_list.set_margin_start(26)
        content.append(self.children_list)

        expanded = window._activity_group_expanded.get(
            self.group_key,
            bool(group.get("is_live")),
        )
        window._activity_group_expanded[self.group_key] = expanded
        self.toggle.set_active(expanded)
        self.toggle.connect("toggled", self._on_toggled)
        self._apply_expanded(expanded)
        self.update_group(group)

    def _on_toggled(self, button: Gtk.ToggleButton) -> None:
        expanded = button.get_active()
        self.window._activity_group_expanded[self.group_key] = expanded
        self._apply_expanded(expanded)
        self.update_group(self._group)

    def _apply_expanded(self, expanded: bool) -> None:
        self.children_list.set_visible(expanded)
        self.arrow.set_from_icon_name(
            "pan-down-symbolic" if expanded else "pan-end-symbolic"
        )

    def _summary(self, group: dict[str, Any]) -> str:
        operation_key = {
            "copy": "sync_group_synchronization",
            "modify": "sync_group_modification",
            "create": "sync_group_creation",
            "rename": "sync_group_rename",
            "delete": "sync_group_deletion",
        }.get(
            str(group.get("operation", "mixed")),
            "sync_group_changes",
        )
        parts = [
            tr(
                operation_key,
                count=int(group.get("item_count", 0) or 0),
            )
        ]
        active = int(group.get("active_count", 0) or 0)
        queued = int(group.get("queued_count", 0) or 0)
        completed = int(group.get("completed_count", 0) or 0)
        errors = int(group.get("error_count", 0) or 0)
        if active or queued:
            parts.append(
                tr(
                    "sync_group_live",
                    active=active,
                    queued=queued,
                )
            )
        elif completed:
            parts.append(tr("sync_group_completed"))
        if errors:
            parts.append(tr("sync_group_errors", count=errors))
        total_size = int(group.get("total_size", 0) or 0)
        if total_size > 0:
            parts.append(human_size(total_size))
        timestamp = self.window.activity_time(
            float(group.get("timestamp", 0) or 0)
        )
        if timestamp:
            parts.append(timestamp)
        return " · ".join(parts)

    def update_group(self, group: dict[str, Any]) -> bool:
        self._group = group
        group_items = group.get("items", [])
        items = (
            list(group_items[:ACTIVITY_GROUP_CHILD_LIMIT])
            if self.toggle.get_active()
            else []
        )
        expected_items = min(
            ACTIVITY_GROUP_CHILD_LIMIT,
            int(group.get("item_count", 0) or 0),
        )
        signature = (
            str(
                group.get(
                    "display_folder_path",
                    group.get("folder_path", ""),
                )
            ),
            str(group.get("folder_path", "")),
            str(group.get("operation", "mixed")),
            int(group.get("item_count", 0) or 0),
            int(group.get("active_count", 0) or 0),
            int(group.get("queued_count", 0) or 0),
            int(group.get("completed_count", 0) or 0),
            int(group.get("error_count", 0) or 0),
            int(group.get("total_size", 0) or 0),
            float(group.get("progress", 0) or 0),
            float(group.get("timestamp", 0) or 0),
            bool(group.get("is_live")),
            self.toggle.get_active(),
            tuple(
                (
                    self._child_key(item),
                    ActivityRow._item_update_signature(item),
                )
                for item in items
            ),
        )
        if signature == self._update_signature:
            if self.toggle.get_active() and len(items) < expected_items:
                self.window.request_activity_group_children(self.group_key)
            return False
        self._update_signature = signature

        folder_text = str(
            group.get(
                "display_folder_path",
                group.get("folder_path", ""),
            )
        )
        if self.folder_label.get_text() != folder_text:
            self.folder_label.set_text(folder_text)
        folder_tooltip = str(group.get("folder_path", ""))
        if self.folder_label.get_tooltip_text() != folder_tooltip:
            self.folder_label.set_tooltip_text(folder_tooltip)
        summary = self._summary(group)
        if self.summary_label.get_text() != summary:
            self.summary_label.set_text(summary)

        show_progress = bool(group.get("is_live")) and int(
            group.get("total_size", 0) or 0
        ) > 0
        progress = float(group.get("progress", 0) or 0)
        progress_paths = frozenset(
            str(item.get("path", ""))
            for item in group.get("items", [])
            if item.get("path")
        )
        if show_progress:
            if (
                self._progress_paths
                and progress_paths.issubset(self._progress_paths)
            ):
                progress = max(progress, self._progress_floor)
            self._progress_floor = progress
            self._progress_paths = progress_paths
        else:
            self._progress_floor = 0.0
            self._progress_paths = frozenset()
        progress_fraction = min(1.0, max(0.0, progress / 100))
        if abs(self.progress.get_fraction() - progress_fraction) >= 0.0001:
            self.progress.set_fraction(progress_fraction)
        progress_text = f"{progress:.0f}%"
        if self.progress.get_text() != progress_text:
            self.progress.set_text(progress_text)
        if self.progress.get_visible() != show_progress:
            self.progress.set_visible(show_progress)

        if self.toggle.get_active() and len(items) < expected_items:
            self.window.request_activity_group_children(self.group_key)
        return self._schedule_child_render(items)

    @staticmethod
    def _child_key(item: dict[str, Any]) -> str:
        return str(
            item.get("event_id")
            or (
                f"{item.get('profile_id', '')}:"
                f"{item.get('operation', '')}:"
                f"{item.get('path', '')}:"
                f"{item.get('timestamp', 0)}"
            )
        )

    def _current_child_rows(self) -> list[ActivityRow]:
        rows: list[ActivityRow] = []
        child = self.children_list.get_first_child()
        while child is not None:
            if isinstance(child, ActivityRow):
                rows.append(child)
            child = child.get_next_sibling()
        return rows

    def _schedule_child_render(
        self,
        items: list[dict[str, Any]],
    ) -> bool:
        desired_keys = [self._child_key(item) for item in items]
        current_rows = self._current_child_rows()
        current_keys = [row.activity_key for row in current_rows]
        if current_keys == desired_keys:
            for row, item in zip(current_rows, items):
                row.update_item(item)
            return False

        self._child_render_generation += 1
        generation = self._child_render_generation
        desired_set = set(desired_keys)
        for row in current_rows:
            if row.activity_key not in desired_set:
                self.children_list.remove(row)
        self._child_render_keys = desired_keys
        self._child_render_items = {
            key: item for key, item in zip(desired_keys, items)
        }
        self._child_render_cursor = 0
        if desired_keys:
            GLib.idle_add(
                self._render_child_batch,
                generation,
                priority=GLib.PRIORITY_DEFAULT_IDLE,
            )
        return True

    def _render_child_batch(self, generation: int) -> bool:
        if (
            generation != self._child_render_generation
            or not self.toggle.get_active()
        ):
            return False

        rows = self._current_child_rows()
        processed = 0
        while (
            self._child_render_cursor < len(self._child_render_keys)
            and processed < ACTIVITY_CHILD_RENDER_BATCH_SIZE
        ):
            index = self._child_render_cursor
            key = self._child_render_keys[index]
            item = self._child_render_items[key]
            if index < len(rows) and rows[index].activity_key == key:
                rows[index].update_item(item)
            else:
                row = next(
                    (
                        candidate
                        for candidate in rows[index:]
                        if candidate.activity_key == key
                    ),
                    None,
                )
                if row is not None:
                    self.children_list.remove(row)
                    rows.remove(row)
                    row.update_item(item)
                else:
                    row = ActivityRow(self.window, item)
                self.children_list.insert(row, index)
                rows.insert(index, row)
            self._child_render_cursor += 1
            processed += 1

        if self._child_render_cursor >= len(self._child_render_keys):
            return False
        return True


def create_application_about_dialog(
    rclone_version_text: str,
) -> Adw.AboutDialog:
    dialog = Adw.AboutDialog()
    dialog.set_title(tr("about_title"))
    dialog.set_application_name(tr("app_title"))
    dialog.set_application_icon(APP_ICON_NAME)
    dialog.set_version(__version__)
    dialog.set_developer_name(__author__)
    dialog.set_developers([__author__])
    dialog.set_copyright(
        tr("about_copyright", author=__author__)
    )
    if __license__ == "MIT":
        dialog.set_license_type(Gtk.License.MIT_X11)
    else:
        dialog.set_license_type(Gtk.License.CUSTOM)
        dialog.set_license(__license__)
    dialog.set_comments(
        tr("about_comments", version=rclone_version_text)
    )
    dialog.add_acknowledgement_section(
        tr("about_acknowledgements"),
        [tr("about_russian_translation_credit")],
    )
    if not _attach_sponsor_card(dialog):
        dialog.add_link(tr("about_support_creator"), SPONSOR_URL)
    dialog.set_debug_info(
        f"{tr('app_title')} {__version__}\n"
        f"rclone {rclone_version_text}"
    )
    dialog.set_debug_info_filename("talaryn-info.txt")
    return dialog


def _valid_timestamp(value: object) -> float:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return 0.0
    return timestamp if timestamp > 0 and math.isfinite(timestamp) else 0.0


def _sponsor_reminder_due_at(state: dict[str, Any]) -> float:
    first_used_at = _valid_timestamp(state.get("sponsor_first_used_at"))
    if not first_used_at:
        return 0.0
    remind_after = _valid_timestamp(state.get("sponsor_remind_after"))
    return remind_after or first_used_at + SPONSOR_INITIAL_DELAY_SECONDS


def _sponsor_reminder_due(
    state: dict[str, Any],
    now: float,
) -> bool:
    if bool(state.get("sponsor_reminder_disabled", False)):
        return False
    due_at = _sponsor_reminder_due_at(state)
    return bool(due_at and now >= due_at)


def _open_sponsor_page(_button: Gtk.Button | None) -> None:
    try:
        Gio.AppInfo.launch_default_for_uri(SPONSOR_URL, None)
    except GLib.Error:
        pass


def _create_sponsor_action_row() -> tuple[Gtk.Box, Gtk.Button]:
    sponsor_row = Gtk.Box(
        orientation=Gtk.Orientation.HORIZONTAL,
        spacing=12,
    )
    sponsor_row.set_margin_start(14)
    sponsor_row.set_margin_end(14)
    sponsor_row.set_margin_top(12)
    sponsor_row.set_margin_bottom(12)

    sponsor_label = Gtk.Label(label=tr("about_github_sponsors"))
    sponsor_label.set_xalign(0)
    sponsor_label.set_hexpand(True)
    sponsor_row.append(sponsor_label)

    button = Gtk.Button()
    button.set_tooltip_text(SPONSOR_URL)
    button_content = Gtk.Box(
        orientation=Gtk.Orientation.HORIZONTAL,
        spacing=6,
    )
    button_content.append(_activity_icon("support-heart.svg", 18))
    button_content.append(Gtk.Label(label=tr("about_sponsor_button")))
    button.set_child(button_content)
    sponsor_row.append(button)
    return sponsor_row, button


def _attach_sponsor_card(dialog: Adw.AboutDialog) -> bool:
    """Place the support card on the main About page, below its version."""
    try:
        version_button = dialog.get_template_child(
            Adw.AboutDialog.__gtype__,
            "version_button",
        )
    except (AttributeError, TypeError, RuntimeError):
        return False
    if not isinstance(version_button, Gtk.Widget):
        return False
    container = version_button.get_parent()
    if not isinstance(container, Gtk.Box):
        return False

    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    card.add_css_class("card")
    card.set_margin_top(18)
    card.set_margin_bottom(6)
    card.set_margin_start(18)
    card.set_margin_end(18)

    heading = Gtk.Label()
    heading.set_markup(
        f"<b>{_escape_pango_markup(tr('about_support_heading'))}</b>"
    )
    heading.set_wrap(True)
    heading.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    heading.set_xalign(0)
    heading.set_margin_top(14)
    heading.set_margin_start(14)
    heading.set_margin_end(14)
    card.append(heading)

    description = Gtk.Label(label=tr("about_support_body"))
    description.set_wrap(True)
    description.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    description.set_xalign(0)
    description.set_margin_start(14)
    description.set_margin_end(14)
    card.append(description)

    sponsor_row, button = _create_sponsor_action_row()
    sponsor_row.set_margin_top(0)
    button.connect("clicked", _open_sponsor_page)
    card.append(sponsor_row)

    container.insert_child_after(card, version_button)
    return True


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Gtk.Application) -> None:
        super().__init__(application=app)
        self.set_title(tr("app_title"))
        if hasattr(self, "set_icon_name"):
            self.set_icon_name(APP_ICON_NAME)
        self.set_default_size(1100, 720)
        self.force_close = False
        self._close_confirmation_open = False
        self.context_menu_open = False
        self.settings = load_settings()
        _apply_color_scheme(str(self.settings.get("color_scheme", "system")))
        self._last_sync_activity: dict[str, Any] = {
            "profiles": {},
            "is_syncing": False,
        }
        self._all_activity_events: list[dict[str, Any]] = []
        self._activity_history_events: list[dict[str, Any]] = []
        self._activity_visible_history_cache = None
        self._activity_history_initialized = False
        self._activity_history_total = 0
        self._activity_history_global_offset = 0
        self._activity_history_profile_totals: dict[str, int] = {}
        self._activity_group_summary_cache: dict[
            tuple[str, str, str, tuple[str, ...]],
            dict[str, dict[str, Any] | None],
        ] = {}
        self._activity_group_header_cache: dict[
            tuple[str, str, str, tuple[str, ...]],
            list[dict[str, Any]],
        ] = {}
        self._activity_group_child_events: dict[
            tuple[tuple[str, str, str, tuple[str, ...]], str],
            list[dict[str, Any]],
        ] = {}
        self._activity_group_child_load_pending: set[
            tuple[tuple[str, str, str, tuple[str, ...]], str]
        ] = set()
        self._activity_render_generation = 0
        self._activity_render_cursor = 0
        self._activity_render_keys: list[str] = []
        self._activity_render_groups: dict[str, dict[str, Any]] = {}
        self._activity_history_summary_signature: tuple[Any, ...] | None = None
        self._activity_history_recent_group_signatures: dict[
            str, tuple[Any, ...]
        ] = {}
        self._activity_profile_summary_cache: dict[
            str, tuple[tuple[Any, ...], dict[str, Any]]
        ] = {}
        self._activity_page_load_pending = False
        self._activity_page_patterns: tuple[str, ...] = ()
        self._activity_page_profile_id = ""
        self._activity_group_expanded: dict[str, bool] = {}
        self._activity_history_cleared_at = 0.0
        self._activity_list_height = ACTIVITY_LIST_DEFAULT_HEIGHT
        self._activity_store: ActivityStore | None = None
        self._pending_unmount: set[str] = set()
        self._mounting_profiles: set[str] = set()
        self._main_scroll_restore_id = 0
        self._sponsor_reminder_source = 0
        self._sponsor_reminder_open = False
        self._sponsor_reminder_shown = False
        self._activity_thumbnail_queue: deque[
            tuple[Gio.File, Gtk.Image, str, str]
        ] = deque()
        self._activity_thumbnail_queries_active = 0
        self._activity_thumbnail_pump_scheduled = False
        self.thumbnail_factory = None
        if GnomeDesktop is not None:
            try:
                self.thumbnail_factory = GnomeDesktop.DesktopThumbnailFactory.new(
                    GnomeDesktop.DesktopThumbnailSize.NORMAL
                )
            except Exception:
                self.thumbnail_factory = None
        self._auto_remounting: set[str] = set()
        self._cleared_unmounted_icons: set[str] = set()
        self.connect("close-request", self._on_close_request)
        ensure_monitor_running()

        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=tr("app_title")))
        toolbar_view.add_top_bar(header)

        remote_manager_button = Gtk.Button(
            icon_name="network-server-symbolic"
        )
        remote_manager_button.set_tooltip_text(tr("remote_manager_title"))
        remote_manager_button.connect(
            "clicked",
            self.show_remote_manager,
        )
        header.pack_start(remote_manager_button)

        settings_button = Gtk.Button(icon_name="emblem-system-symbolic")
        settings_button.set_tooltip_text(tr("settings"))
        settings_button.connect("clicked", self.show_settings)
        header.pack_end(settings_button)

        self.help_button = Gtk.Button(icon_name="help-about-symbolic")
        self.help_button.set_tooltip_text(tr("help"))
        self.help_button.connect("clicked", self.show_about)
        header.pack_end(self.help_button)

        self.main_scrolled = Gtk.ScrolledWindow()
        self.main_scrolled.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC,
        )
        self.main_scrolled.set_min_content_height(120)
        self.main_scrolled.set_propagate_natural_height(False)
        self.main_scrolled.set_hexpand(True)
        self.main_scrolled.set_vexpand(True)
        toolbar_view.set_content(self.main_scrolled)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)
        content.set_hexpand(True)

        self.main_scrolled.set_child(content)

        dashboard = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=18,
        )
        dashboard.set_hexpand(True)
        dashboard.set_valign(Gtk.Align.START)
        content.append(dashboard)

        self.mounted_section = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
        )
        self.mounted_section.set_size_request(215, -1)
        self.mounted_section.set_hexpand(False)
        self.mounted_section.set_halign(Gtk.Align.START)
        self.mounted_section.set_valign(Gtk.Align.START)
        dashboard.append(self.mounted_section)

        self.mounted_label = Gtk.Label(
            label=tr("mounted_title"),
            xalign=0,
        )
        self.mounted_label.add_css_class("title-2")
        self.mounted_section.append(self.mounted_label)

        self.mounted_box = Gtk.FlowBox()
        self.mounted_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.mounted_box.set_max_children_per_line(1)
        self.mounted_box.set_min_children_per_line(1)
        self.mounted_box.set_homogeneous(True)
        self.mounted_box.set_row_spacing(4)
        self.mounted_box.set_valign(Gtk.Align.START)
        self.mounted_section.append(self.mounted_box)

        sync_section = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
        )
        sync_section.set_hexpand(True)
        sync_section.set_valign(Gtk.Align.START)
        dashboard.append(sync_section)

        speed_title = Gtk.Label(label=tr("transfer_speed_title"), xalign=0)
        speed_title.add_css_class("title-2")
        sync_section.append(speed_title)

        speed_frame = Gtk.Frame()
        sync_section.append(speed_frame)
        speed_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        speed_box.set_homogeneous(True)
        speed_box.set_margin_top(10)
        speed_box.set_margin_bottom(10)
        speed_box.set_margin_start(12)
        speed_box.set_margin_end(12)
        speed_frame.set_child(speed_box)

        self.total_speed_label = self._build_speed_metric(
            speed_box,
            tr("transfer_speed_total"),
            "network-transmit-receive-symbolic",
        )
        self.upload_speed_label = self._build_speed_metric(
            speed_box,
            tr("transfer_speed_upload"),
            "go-up-symbolic",
        )
        self.download_speed_label = self._build_speed_metric(
            speed_box,
            tr("transfer_speed_download"),
            "go-down-symbolic",
        )

        self.speed_chart = Gtk.DrawingArea()
        self.speed_chart.set_content_height(72)
        self.speed_chart.set_hexpand(True)
        self.speed_chart.set_draw_func(self._draw_speed_chart)

        speed_legend = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=16,
        )
        speed_legend.set_halign(Gtk.Align.END)
        self._append_speed_legend_item(
            speed_legend,
            tr("transfer_speed_download"),
            DOWNLOAD_CHART_COLOR,
        )
        self._append_speed_legend_item(
            speed_legend,
            tr("transfer_speed_upload"),
            UPLOAD_CHART_COLOR,
        )
        sync_section.append(speed_legend)
        sync_section.append(self.speed_chart)

        self.activity_label = Gtk.Label(label=tr("sync_activity_title"), xalign=0)
        self.activity_label.add_css_class("title-2")
        sync_section.append(self.activity_label)

        self.activity_summary = Gtk.Label(xalign=0)
        self.activity_summary.set_wrap(True)
        self.activity_summary.add_css_class("dim-label")
        sync_section.append(self.activity_summary)

        self.health_warning = Gtk.Label(xalign=0)
        self.health_warning.set_wrap(True)
        self.health_warning.add_css_class("error")
        self.health_warning.set_visible(False)
        sync_section.append(self.health_warning)

        filters = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sync_section.append(filters)
        self._profile_filter_ids = [""]
        self.profile_filter = Gtk.DropDown(
            model=Gtk.StringList.new([tr("filter_all_drives")])
        )
        self.profile_filter.connect(
            "notify::selected", self._on_activity_filter_changed
        )
        filters.append(self.profile_filter)
        operation_filters = (
            ("", tr("filter_all_operations")),
            ("copy", tr("filter_copy")),
            ("modify", tr("filter_modify")),
            ("create", tr("filter_create")),
            ("rename", tr("filter_rename")),
            ("delete", tr("filter_delete")),
        )
        self._operation_filter_ids = [item[0] for item in operation_filters]
        self.operation_filter = Gtk.DropDown(
            model=Gtk.StringList.new([item[1] for item in operation_filters])
        )
        self.operation_filter.connect(
            "notify::selected", self._on_activity_filter_changed
        )
        filters.append(self.operation_filter)
        state_filters = (
            ("", tr("filter_all_states")),
            ("active", tr("filter_active")),
            ("queued", tr("filter_queued")),
            ("completed", tr("filter_completed")),
            ("error", tr("filter_errors")),
        )
        self._state_filter_ids = [item[0] for item in state_filters]
        self.state_filter = Gtk.DropDown(
            model=Gtk.StringList.new([item[1] for item in state_filters])
        )
        self.state_filter.connect(
            "notify::selected", self._on_activity_filter_changed
        )
        filters.append(self.state_filter)

        self.show_hidden_activity = Gtk.CheckButton(label=tr("activity_show_hidden"))
        self.show_hidden_activity.set_tooltip_text(tr("activity_show_hidden_help"))
        self.show_hidden_activity.connect("toggled", self._on_activity_visibility_changed)
        self.show_hidden_activity.set_valign(Gtk.Align.CENTER)
        filters.append(self.show_hidden_activity)

        activity_container = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=0,
        )
        activity_container.add_css_class("card")
        activity_container.set_overflow(Gtk.Overflow.HIDDEN)
        sync_section.append(activity_container)

        self.activity_scrolled = Gtk.ScrolledWindow()
        self.activity_scrolled.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC,
        )
        self.activity_scrolled.set_size_request(
            -1,
            self._activity_list_height,
        )
        self.activity_scrolled.set_overlay_scrolling(False)
        activity_container.append(self.activity_scrolled)

        self.activity_list = Gtk.ListBox()
        self.activity_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.activity_list.set_show_separators(True)
        self.activity_scrolled.set_child(self.activity_list)
        self._activity_scroll_restore_id = 0
        self.activity_scrolled.get_vadjustment().connect(
            "value-changed",
            self._on_activity_scroll_changed,
        )

        activity_container.append(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        )
        activity_footer = Gtk.CenterBox()
        activity_footer.set_margin_top(6)
        activity_footer.set_margin_bottom(6)
        activity_footer.set_margin_start(8)
        activity_footer.set_margin_end(8)
        activity_container.append(activity_footer)

        self.load_older_activity_button = Gtk.Button()
        self.load_older_activity_button.set_halign(Gtk.Align.CENTER)
        self.load_older_activity_button.add_css_class("flat")
        self.load_older_activity_button.connect(
            "clicked",
            self._request_more_activity,
        )
        self.load_older_activity_button.set_visible(False)
        activity_footer.set_center_widget(self.load_older_activity_button)

        activity_height_controls = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=0,
        )
        activity_height_controls.add_css_class("linked")
        activity_footer.set_end_widget(activity_height_controls)

        self.decrease_activity_height_button = Gtk.Button(label="−")
        self.decrease_activity_height_button.add_css_class("flat")
        self.decrease_activity_height_button.set_tooltip_text(
            tr("decrease_activity_list_height")
        )
        self.decrease_activity_height_button.connect(
            "clicked",
            self._change_activity_list_height,
            -ACTIVITY_LIST_HEIGHT_STEP,
        )
        activity_height_controls.append(
            self.decrease_activity_height_button
        )

        self.increase_activity_height_button = Gtk.Button(label="+")
        self.increase_activity_height_button.add_css_class("flat")
        self.increase_activity_height_button.set_tooltip_text(
            tr("increase_activity_list_height")
        )
        self.increase_activity_height_button.connect(
            "clicked",
            self._change_activity_list_height,
            ACTIVITY_LIST_HEIGHT_STEP,
        )
        activity_height_controls.append(
            self.increase_activity_height_button
        )
        self._update_activity_height_buttons()

        self.history_summary = Gtk.Label(xalign=0)
        self.history_summary.set_wrap(True)
        self.history_summary.add_css_class("dim-label")
        self.history_summary.add_css_class("caption")
        sync_section.append(self.history_summary)

        self.unmounted_label = Gtk.Label(label=tr("unmounted_title"), xalign=0)
        self.unmounted_label.add_css_class("title-2")
        content.append(self.unmounted_label)

        self.unmounted_box = Gtk.FlowBox()
        self.unmounted_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.unmounted_box.set_max_children_per_line(4)
        self.unmounted_box.set_min_children_per_line(1)
        self.unmounted_box.set_homogeneous(True)
        self.unmounted_box.set_halign(Gtk.Align.FILL)
        self.unmounted_box.set_hexpand(True)
        self.unmounted_box.set_column_spacing(6)
        self.unmounted_box.set_row_spacing(6)
        content.append(self.unmounted_box)

        self.empty_label = Gtk.Label(label=tr("no_profiles"), xalign=0)
        self.empty_label.set_wrap(True)
        self.empty_label.add_css_class("dim-label")
        content.append(self.empty_label)

        self.refresh()
        self._ensure_tray_running()
        if self.settings.get("automount_previous"):
            GLib.idle_add(self._automount_previous_profiles)
        GLib.timeout_add_seconds(1, self._periodic_activity_refresh)
        GLib.timeout_add_seconds(5, self._periodic_refresh)

    def _periodic_activity_refresh(self) -> bool:
        if (
            self.get_visible()
            and self.get_mapped()
            and not self.context_menu_open
        ):
            self._refresh_sync_activity()
        return True

    def _periodic_refresh(self) -> bool:
        if (
            not self.get_visible()
            or not self.get_mapped()
            or self.context_menu_open
        ):
            return True

        self.refresh(refresh_activity=False)
        if self.settings.get("auto_remount"):
            self._auto_remount_profiles()
        return True

    def _on_close_request(self, _window: Gtk.Window) -> bool:
        if self.force_close:
            return False

        if (
            self.settings.get("minimize_to_tray")
            and not self.settings.get("disable_tray_icon")
        ):
            self.set_visible(False)
            self._ensure_tray_running()
            return True
        if self.confirm_close_if_needed(self._close_after_confirmation):
            return True
        return False

    def _close_after_confirmation(self) -> None:
        self.force_close = True
        self.close()

    def _sync_activity_for_close(self) -> dict[str, Any]:
        try:
            activity = get_sync_activity()
        except Exception:
            activity = self._last_sync_activity
        return activity if isinstance(activity, dict) else {}

    def confirm_close_if_needed(
        self,
        on_confirmed: Callable[[], None],
    ) -> bool:
        if not self.settings.get("confirm_close_during_sync", False):
            return False
        activity = self._sync_activity_for_close()
        if not activity.get("is_syncing"):
            return False
        if self._close_confirmation_open:
            return True

        self._close_confirmation_open = True
        dialog = Adw.AlertDialog.new(
            tr("close_during_sync_title"),
            tr(
                "close_during_sync_body",
                active=int(activity.get("total_active_count", 0) or 0),
                queued=int(activity.get("queued_count", 0) or 0),
            ),
        )
        dialog.add_response("cancel", tr("cancel"))
        dialog.add_response("close", tr("close_app_anyway"))
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance(
            "close",
            Adw.ResponseAppearance.DESTRUCTIVE,
        )
        dialog.connect(
            "response",
            self._on_close_confirmation_response,
            on_confirmed,
        )
        dialog.present(self)
        return True

    def _on_close_confirmation_response(
        self,
        _dialog: Adw.AlertDialog,
        response: str,
        on_confirmed: Callable[[], None],
    ) -> None:
        self._close_confirmation_open = False
        if response == "close":
            on_confirmed()

    def _ensure_tray_running(self) -> None:
        if self.settings.get("disable_tray_icon"):
            return

        try:
            subprocess.Popen(
                application_command("tray"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def _automount_previous_profiles(self) -> bool:
        profiles = list_profiles()
        for profile_id in sorted(desired_mounted_profiles()):
            profile = profiles.get(profile_id)
            if profile and not is_profile_mounted(profile):
                self.mount_profile(profile_id, open_after_mount=False)
        return False

    def _auto_remount_profiles(self) -> None:
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
        self.refresh()
        return False

    def set_context_menu_open(self, is_open: bool) -> None:
        self.context_menu_open = is_open

    def _clear_flowbox(self, flowbox: Gtk.FlowBox) -> None:
        child = flowbox.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            flowbox.remove(child)
            child = next_child

    def _build_speed_metric(
        self,
        parent: Gtk.Box,
        title: str,
        icon_name: str,
    ) -> Gtk.Label:
        metric = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        metric.set_hexpand(True)
        parent.append(metric)

        icon = Gtk.Image(icon_name=icon_name)
        icon.set_pixel_size(24)
        metric.append(icon)

        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        metric.append(labels)

        title_label = Gtk.Label(label=title, xalign=0)
        title_label.add_css_class("dim-label")
        title_label.add_css_class("caption")
        labels.append(title_label)

        value_label = Gtk.Label(label=f"{human_size(0)}/s", xalign=0)
        value_label.add_css_class("title-3")
        value_label.add_css_class("numeric")
        labels.append(value_label)
        return value_label

    def _append_speed_legend_item(
        self,
        parent: Gtk.Box,
        title: str,
        color: tuple[float, float, float, float],
    ) -> None:
        item = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        parent.append(item)

        swatch = Gtk.DrawingArea()
        swatch.set_content_width(18)
        swatch.set_content_height(10)
        swatch.set_draw_func(
            lambda _area, context, width, height, shade=color: (
                self._draw_speed_legend_swatch(
                    context,
                    width,
                    height,
                    shade,
                )
            )
        )
        item.append(swatch)

        label = Gtk.Label(label=title)
        label.add_css_class("caption")
        label.add_css_class("dim-label")
        item.append(label)

    def _draw_speed_legend_swatch(
        self,
        context: Any,
        width: int,
        height: int,
        color: tuple[float, float, float, float],
    ) -> None:
        context.set_source_rgba(*color)
        context.set_line_width(3)
        context.move_to(1, height / 2)
        context.line_to(max(1, width - 1), height / 2)
        context.stroke()

    def _draw_speed_chart(
        self,
        _area: Gtk.DrawingArea,
        context: Any,
        width: int,
        height: int,
    ) -> None:
        history = self._last_sync_activity.get("speed_history", [])
        padding = 5.0
        usable_width = max(1.0, width - 2 * padding)
        usable_height = max(1.0, height - 2 * padding)

        context.set_line_width(1)
        context.set_source_rgba(0.5, 0.5, 0.5, 0.18)
        for row in range(5):
            y = padding + usable_height * row / 4
            context.move_to(padding, y)
            context.line_to(width - padding, y)
        for column in range(7):
            x = padding + usable_width * column / 6
            context.move_to(x, padding)
            context.line_to(x, height - padding)
        context.stroke()

        if not isinstance(history, list) or len(history) < 2:
            return
        maximum = max(
            1.0,
            *(
                float(item.get("upload", 0) or 0)
                + float(item.get("download", 0) or 0)
                for item in history
                if isinstance(item, dict)
            ),
        )

        for field, color in (
            ("download", DOWNLOAD_CHART_COLOR),
            ("upload", UPLOAD_CHART_COLOR),
        ):
            context.set_source_rgba(*color)
            context.set_line_width(2)
            for index, item in enumerate(history):
                x = padding + usable_width * index / max(1, len(history) - 1)
                value = (
                    float(item.get(field, 0) or 0)
                    if isinstance(item, dict)
                    else 0
                )
                y = height - padding - usable_height * value / maximum
                if index == 0:
                    context.move_to(x, y)
                else:
                    context.line_to(x, y)
            context.stroke()

    def _clear_listbox(self, listbox: Gtk.ListBox) -> None:
        child = listbox.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            listbox.remove(child)
            child = next_child

    def activity_time(self, timestamp: float) -> str:
        if timestamp <= 0:
            return ""
        value = datetime.fromtimestamp(timestamp)
        now = datetime.now()
        if value.date() == now.date():
            return value.strftime("%H:%M")
        return value.strftime("%Y-%m-%d %H:%M")

    def _append_empty_activity(
        self,
        listbox: Gtk.ListBox,
        text: str,
    ) -> None:
        row = Adw.ActionRow(title=text)
        row.set_sensitive(False)
        listbox.append(row)

    @staticmethod
    def _activity_history_key(item: dict[str, Any]) -> str:
        return str(
            item.get("event_id")
            or (
                f"{item.get('profile_id', '')}:"
                f"{item.get('operation', '')}:"
                f"{item.get('path', '')}:"
                f"{item.get('timestamp', 0)}"
            )
        )

    def _merge_activity_history(
        self,
        items: list[dict[str, Any]],
    ) -> None:
        by_key = {
            self._activity_history_key(item): item
            for item in self._activity_history_events
        }
        for item in items:
            if isinstance(item, dict):
                by_key[self._activity_history_key(item)] = item
        events = sorted(
            by_key.values(),
            key=lambda item: (
                float(item.get("timestamp", 0) or 0),
                self._activity_history_key(item),
            ),
            reverse=True,
        )
        if len(events) <= ACTIVITY_HISTORY_MEMORY_LIMIT:
            self._activity_history_events = events
            return

        selected_profile = ""
        if hasattr(self, "profile_filter"):
            selected_profile = self._selected_filter(
                self.profile_filter,
                self._profile_filter_ids,
            )
        if not selected_profile:
            self._activity_history_events = events[
                :ACTIVITY_HISTORY_MEMORY_LIMIT
            ]
            return

        selected_events = [
            item
            for item in events
            if str(item.get("profile_id", "")) == selected_profile
        ][:ACTIVITY_HISTORY_MEMORY_LIMIT]
        selected_keys = {
            self._activity_history_key(item) for item in selected_events
        }
        remaining = ACTIVITY_HISTORY_MEMORY_LIMIT - len(selected_events)
        other_events = [
            item
            for item in events
            if self._activity_history_key(item) not in selected_keys
        ][:remaining]
        self._activity_history_events = sorted(
            [*selected_events, *other_events],
            key=lambda item: (
                float(item.get("timestamp", 0) or 0),
                self._activity_history_key(item),
            ),
            reverse=True,
        )

    def _sync_activity_history(self, activity: dict[str, Any]) -> None:
        recent = [
            item
            for item in activity.get("recent", [])
            if isinstance(item, dict)
        ]
        try:
            history_total = int(activity["history_total"])
        except (KeyError, TypeError, ValueError):
            try:
                history_total = ActivityStore().activity_count()
            except Exception:
                history_total = max(
                    len(recent),
                    len(self._activity_history_events),
                )

        event_signatures = tuple(
            (
                self._activity_history_key(item),
                str(item.get("state", "")),
                str(item.get("operation", "")),
                int(item.get("size", 0) or 0),
                float(item.get("group_timestamp", 0) or 0),
                str(item.get("error", "")),
            )
            for item in recent
        )
        patterns = self._activity_hidden_patterns()
        if patterns:
            revision = history_total, event_signatures, patterns
            cached = self._activity_visible_history_cache
            if cached is None or cached[0] != revision:
                try:
                    if self._activity_store is None:
                        self._activity_store = ActivityStore()
                    visible_recent = self._activity_store.recent(
                        ACTIVITY_HISTORY_PAGE_SIZE, hidden_patterns=patterns,
                    )
                    visible_total = self._activity_store.activity_count(
                        hidden_patterns=patterns,
                    )
                    cached = revision, visible_recent, visible_total
                    self._activity_visible_history_cache = cached
                except Exception:
                    cached = None
            if cached is not None:
                recent, history_total = cached[1], cached[2]
            else:
                recent = [item for item in recent if activity_is_visible(item, patterns)]
                history_total = len(recent)
            event_signatures = tuple(
                (self._activity_history_key(item), str(item.get("state", "")),
                 str(item.get("operation", "")), int(item.get("size", 0) or 0),
                 float(item.get("group_timestamp", 0) or 0), str(item.get("error", "")))
                for item in recent
            )
        summary_signature = history_total, event_signatures
        grouped_signatures: dict[str, list[tuple[Any, ...]]] = {}
        for item, event_signature in zip(recent, event_signatures):
            grouped_signatures.setdefault(activity_group_key(item), []).append(
                event_signature
            )
        recent_group_signatures = {
            key: tuple(signatures)
            for key, signatures in grouped_signatures.items()
        }
        if summary_signature != self._activity_history_summary_signature:
            self._activity_history_profile_totals.clear()
            self._activity_group_header_cache.clear()
            if history_total < self._activity_history_total:
                self._activity_group_summary_cache.clear()
            else:
                changed_group_keys = {
                    key
                    for key in (
                        self._activity_history_recent_group_signatures.keys()
                        | recent_group_signatures.keys()
                    )
                    if self._activity_history_recent_group_signatures.get(key)
                    != recent_group_signatures.get(key)
                }
                for cache in self._activity_group_summary_cache.values():
                    for key in changed_group_keys:
                        cache.pop(key, None)
            self._activity_history_recent_group_signatures = (
                recent_group_signatures
            )
            self._activity_history_summary_signature = summary_signature

        existing_keys = {
            self._activity_history_key(item)
            for item in self._activity_history_events
        }
        recent_keys = {
            self._activity_history_key(item)
            for item in recent
        }
        latest_page_no_longer_overlaps = bool(
            existing_keys
            and recent_keys
            and existing_keys.isdisjoint(recent_keys)
        )
        if latest_page_no_longer_overlaps:
            self._activity_group_summary_cache.clear()
        if (
            not self._activity_history_initialized
            or history_total < len(self._activity_history_events)
            or latest_page_no_longer_overlaps
        ):
            self._activity_history_events = []
            self._activity_history_initialized = True
            self._activity_history_global_offset = 0
            self._activity_history_profile_totals = {}
        new_recent_count = len(recent_keys - existing_keys)
        self._merge_activity_history(recent)
        if self._activity_history_global_offset == 0:
            self._activity_history_global_offset = len(recent)
        else:
            self._activity_history_global_offset += new_recent_count
        self._activity_history_total = max(
            history_total,
            len(self._activity_history_events),
        )
        self._update_activity_pagination(refresh_total=not bool(patterns))

    def _compose_activity_events(self, activity: dict[str, Any]) -> None:
        active = [
            item
            for item in activity.get("active", [])
            if isinstance(item, dict)
        ]
        queued = [
            item
            for item in activity.get("queued", [])
            if isinstance(item, dict)
        ]
        self._all_activity_events = [
            *active,
            *queued,
            *self._activity_history_events,
        ]

    def _change_activity_list_height(
        self,
        _button: Gtk.Button,
        change: int,
    ) -> None:
        new_height = min(
            ACTIVITY_LIST_MAX_HEIGHT,
            max(
                ACTIVITY_LIST_MIN_HEIGHT,
                self._activity_list_height + change,
            ),
        )
        if new_height == self._activity_list_height:
            return

        scroll_state = self._capture_activity_scroll()
        self._activity_list_height = new_height
        self.activity_scrolled.set_size_request(-1, new_height)
        self._update_activity_height_buttons()
        self._schedule_activity_scroll_restore(scroll_state)

    def _update_activity_height_buttons(self) -> None:
        self.decrease_activity_height_button.set_sensitive(
            self._activity_list_height > ACTIVITY_LIST_MIN_HEIGHT
        )
        self.increase_activity_height_button.set_sensitive(
            self._activity_list_height < ACTIVITY_LIST_MAX_HEIGHT
        )

    def _activity_pagination_state(
        self,
        refresh_total: bool = False,
    ) -> tuple[str, int, int]:
        profile_id = self._selected_filter(
            self.profile_filter,
            self._profile_filter_ids,
        )
        if not profile_id:
            if len(self._activity_history_events) >= (
                ACTIVITY_HISTORY_MEMORY_LIMIT
            ):
                return "", self._activity_history_total, self._activity_history_total
            return (
                "",
                self._activity_history_total,
                min(
                    self._activity_history_global_offset,
                    self._activity_history_total,
                ),
            )

        loaded = sum(
            1
            for item in self._activity_history_events
            if str(item.get("profile_id", "")) == profile_id
        )
        total = self._activity_history_profile_totals.get(profile_id)
        if total is None or refresh_total:
            try:
                if self._activity_store is None:
                    self._activity_store = ActivityStore()
                total = self._activity_store.activity_count(
                    profile_id, hidden_patterns=self._activity_hidden_patterns(),
                )
                self._activity_history_profile_totals[profile_id] = total
            except Exception:
                total = loaded
        if loaded >= ACTIVITY_HISTORY_MEMORY_LIMIT:
            return profile_id, max(total, loaded), max(total, loaded)
        return profile_id, max(total, loaded), loaded

    def _update_activity_pagination(
        self,
        refresh_total: bool = False,
    ) -> None:
        _profile_id, total, loaded = self._activity_pagination_state(
            refresh_total
        )
        remaining = max(0, total - loaded)
        page_count = min(ACTIVITY_HISTORY_PAGE_SIZE, remaining)
        self.load_older_activity_button.set_label(
            tr("load_older_activity", count=page_count)
        )
        self.load_older_activity_button.set_visible(remaining > 0)
        self.load_older_activity_button.set_sensitive(
            remaining > 0 and not self._activity_page_load_pending
        )

    def _on_activity_scroll_changed(
        self,
        adjustment: Gtk.Adjustment,
    ) -> None:
        if self._activity_page_load_pending:
            return
        _profile_id, total, loaded = self._activity_pagination_state()
        if loaded >= total:
            return
        page_size = adjustment.get_page_size()
        if adjustment.get_upper() <= page_size + 1:
            return
        remaining = (
            adjustment.get_upper()
            - adjustment.get_value()
            - page_size
        )
        if remaining <= max(80.0, page_size * 0.5):
            self._request_more_activity()

    def _request_more_activity(self, *_args: Any) -> None:
        if self._activity_page_load_pending:
            return
        profile_id, total, loaded = self._activity_pagination_state()
        if loaded >= total:
            return
        self._activity_page_load_pending = True
        self._activity_page_profile_id = profile_id
        self._activity_page_patterns = self._activity_hidden_patterns()
        self._update_activity_pagination()
        GLib.idle_add(self._load_more_activity_page)

    def _load_more_activity_page(self) -> bool:
        if self._activity_page_patterns != self._activity_hidden_patterns():
            self._activity_page_load_pending = False
            self._activity_page_profile_id = ""
            self._update_activity_pagination()
            return False
        scroll_state = self._capture_activity_scroll()
        profile_id = self._activity_page_profile_id
        if profile_id:
            offset = sum(
                1
                for item in self._activity_history_events
                if str(item.get("profile_id", "")) == profile_id
            )
        else:
            offset = self._activity_history_global_offset
        page: list[dict[str, Any]] = []
        try:
            if self._activity_store is None:
                self._activity_store = ActivityStore()
            store = self._activity_store
            page = store.recent(
                ACTIVITY_HISTORY_PAGE_SIZE,
                profile_id=profile_id or None,
                offset=offset,
                hidden_patterns=self._activity_hidden_patterns(),
            )
            self._merge_activity_history(page)
            if profile_id:
                self._activity_history_profile_totals[profile_id] = (
                    store.activity_count(
                        profile_id, hidden_patterns=self._activity_hidden_patterns(),
                    )
                )
            else:
                self._activity_history_global_offset += len(page)
                self._activity_history_total = max(
                    store.activity_count(hidden_patterns=self._activity_hidden_patterns()),
                    self._activity_history_global_offset,
                )
        except Exception:
            pass
        finally:
            self._activity_page_load_pending = False
            self._activity_page_profile_id = ""

        self._compose_activity_events(self._last_sync_activity)
        structure_changed = self._render_filtered_activity()
        self._update_activity_pagination()
        if structure_changed:
            self._schedule_activity_scroll_restore(scroll_state)
        return False

    def _refresh_sync_activity(self) -> None:
        scroll_state = self._capture_activity_scroll()
        activity = get_sync_activity()
        if (
            self._activity_history_cleared_at
            and float(activity.get("updated_at", 0) or 0)
            <= self._activity_history_cleared_at
        ):
            activity = self._without_activity_history(activity)
        self._sync_activity_history(activity)
        self._last_sync_activity = activity

        if activity["is_syncing"]:
            self.activity_summary.set_text(
                tr(
                    "sync_summary",
                    active=activity["total_active_count"],
                    queued=activity["queued_count"],
                )
            )
        else:
            self.activity_summary.set_text(tr("sync_idle"))

        self.total_speed_label.set_text(
            f"{human_size(float(activity['total_speed']))}/s"
        )
        self.upload_speed_label.set_text(
            f"{human_size(float(activity['upload_speed']))}/s"
        )
        self.download_speed_label.set_text(
            f"{human_size(float(activity['download_speed']))}/s"
        )
        self.speed_chart.queue_draw()

        warnings: list[str] = []
        profiles = list_profiles()
        for profile_id, state in activity.get("profiles", {}).items():
            label = str(profiles.get(profile_id, {}).get("label", profile_id))
            if state.get("cache_out_of_space"):
                warnings.append(tr("cache_out_of_space", profile=label))
            error_files = int(state.get("cache_error_files", 0) or 0)
            if error_files:
                warnings.append(
                    tr("cache_error_files", profile=label, count=error_files)
                )
            if state.get("fatal_error") and state.get("last_error"):
                warnings.append(f"{label}: {state['last_error']}")
        self.health_warning.set_text("\n".join(warnings))
        self.health_warning.set_visible(bool(warnings))

        self._update_profile_filter()
        self._update_history_summary(activity)
        self._compose_activity_events(activity)
        structure_changed = self._render_filtered_activity()
        if structure_changed:
            self._schedule_activity_scroll_restore(scroll_state)

        for profile_id in list(self._pending_unmount):
            if snapshot_allows_unmount(activity, profile_id):
                self.unmount_profile(profile_id)

    def _update_history_summary(
        self,
        activity: dict[str, Any],
    ) -> None:
        profile_id = self._selected_filter(
            self.profile_filter,
            self._profile_filter_ids,
        )
        summaries = activity.get("history_summary", {})
        if profile_id:
            revision = (
                self._activity_history_summary_signature,
                int(time.time() // 60),
            )
            cached = self._activity_profile_summary_cache.get(profile_id)
            if cached is not None and cached[0] == revision:
                summaries = cached[1]
            else:
                try:
                    if self._activity_store is None:
                        self._activity_store = ActivityStore()
                    summaries = self._activity_store.summary(profile_id)
                except Exception:
                    summaries = {}
                else:
                    self._activity_profile_summary_cache[profile_id] = (
                        revision,
                        summaries,
                    )
        summary = summaries.get("day", {})
        month = summaries.get("month", {})
        self.history_summary.set_markup(
            tr(
                "history_summary",
                day_count=int(summary.get("completed", 0) or 0),
                day_size=human_size(float(summary.get("bytes", 0) or 0)),
                day_errors=int(summary.get("errors", 0) or 0),
                month_count=int(month.get("completed", 0) or 0),
                month_size=human_size(float(month.get("bytes", 0) or 0)),
                month_errors=int(month.get("errors", 0) or 0),
            )
        )

    def _update_profile_filter(self) -> None:
        profiles = list_profiles()
        current_index = int(self.profile_filter.get_selected())
        current_id = (
            self._profile_filter_ids[current_index]
            if current_index < len(self._profile_filter_ids)
            else ""
        )
        new_ids = ["", *sorted(profiles)]
        if new_ids == self._profile_filter_ids:
            return
        labels = [tr("filter_all_drives")] + [
            str(profiles[profile_id].get("label", profile_id))
            for profile_id in new_ids[1:]
        ]
        self._profile_filter_ids = new_ids
        self.profile_filter.set_model(Gtk.StringList.new(labels))
        self.profile_filter.set_selected(
            new_ids.index(current_id) if current_id in new_ids else 0
        )

    def _activity_hidden_patterns(self) -> tuple[str, ...]:
        if self.show_hidden_activity.get_active():
            return ()
        return tuple(normalize_activity_patterns(self.settings.get("activity_hidden_patterns")))

    def _on_activity_visibility_changed(self, *_args: Any) -> None:
        self._activity_history_events = []
        self._activity_visible_history_cache = None
        self._activity_history_initialized = False
        self._activity_history_total = 0
        self._activity_history_global_offset = 0
        self._activity_history_profile_totals.clear()
        self._activity_group_summary_cache.clear()
        self._activity_group_header_cache.clear()
        self._activity_group_child_events.clear()
        self._activity_group_child_load_pending.clear()
        self._activity_history_summary_signature = None
        self._activity_history_recent_group_signatures = {}
        self._activity_profile_summary_cache.clear()
        self._refresh_sync_activity()

    def _on_activity_filter_changed(self, *_args: Any) -> None:
        if hasattr(self, "activity_list"):
            self._render_filtered_activity()
            self._update_history_summary(self._last_sync_activity)
            self._update_activity_pagination(refresh_total=True)

    def _selected_filter(self, dropdown: Gtk.DropDown, values: list[str]) -> str:
        index = int(dropdown.get_selected())
        return values[index] if 0 <= index < len(values) else ""

    def _activity_filter_key(self) -> tuple[str, str, str]:
        return (
            self._selected_filter(
                self.profile_filter, self._profile_filter_ids
            ),
            self._selected_filter(
                self.operation_filter, self._operation_filter_ids
            ),
            self._selected_filter(
                self.state_filter, self._state_filter_ids
            ),
        )

    def _activity_history_filter_key(
        self,
    ) -> tuple[str, str, str, tuple[str, ...]] | None:
        profile_id, operation, state_filter = self._activity_filter_key()
        if state_filter in ("active", "queued"):
            return None
        history_state = (
            state_filter if state_filter in ("completed", "error") else ""
        )
        return profile_id, operation, history_state, self._activity_hidden_patterns()

    def _filtered_activity_events(self) -> list[dict[str, Any]]:
        profile_id, operation, state_filter = self._activity_filter_key()
        cached_events: list[dict[str, Any]] = []
        history_filter_key = self._activity_history_filter_key()
        if history_filter_key is not None:
            for (filter_key, _group_key), items in (
                self._activity_group_child_events.items()
            ):
                if filter_key == history_filter_key:
                    cached_events.extend(items)
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        patterns = self._activity_hidden_patterns()
        for item in [*self._all_activity_events, *cached_events]:
            if not activity_is_visible(item, patterns):
                continue
            if profile_id and item.get("profile_id") != profile_id:
                continue
            if operation and item.get("operation") != operation:
                continue
            state = str(item.get("state", "completed"))
            if state_filter == "active" and state not in ("uploading", "retrying"):
                continue
            if state_filter == "queued" and state != "queued":
                continue
            if state_filter == "completed" and state != "completed":
                continue
            if state_filter == "error" and state != "error":
                continue
            event_key = self._activity_history_key(item)
            if event_key in seen:
                continue
            seen.add(event_key)
            result.append(item)
        return result

    def _recent_activity_group_headers(self) -> list[dict[str, Any]]:
        filter_key = self._activity_history_filter_key()
        if filter_key is None:
            return []
        cached = self._activity_group_header_cache.get(filter_key)
        if cached is not None:
            return cached
        profile_id, operation, history_state, patterns = filter_key
        try:
            if self._activity_store is None:
                self._activity_store = ActivityStore()
            headers = self._activity_store.recent_group_headers(
                ACTIVITY_INITIAL_GROUP_LIMIT,
                profile_id=profile_id or None,
                operation=operation or None,
                state=history_state or None,
                hidden_patterns=patterns,
            )
        except Exception:
            return []
        self._activity_group_header_cache[filter_key] = headers
        summary_cache = self._activity_group_summary_cache.setdefault(
            filter_key, {}
        )
        for header in headers:
            summary_cache[str(header.get("group_key", ""))] = header
        return headers

    def _complete_activity_group_summaries(
        self,
        groups: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        filter_key = self._activity_history_filter_key()
        if filter_key is None:
            return {}
        profile_id, operation, history_state, patterns = filter_key
        cache = self._activity_group_summary_cache.setdefault(filter_key, {})
        group_keys = [str(group.get("group_key", "")) for group in groups]
        missing_keys = [
            key for key in group_keys if key and key not in cache
        ]
        if missing_keys:
            try:
                if self._activity_store is None:
                    self._activity_store = ActivityStore()
                summaries = self._activity_store.group_summaries(
                    missing_keys,
                    profile_id=profile_id or None,
                    operation=operation or None,
                    state=history_state or None,
                    hidden_patterns=patterns,
                )
            except Exception:
                summaries = {}
            else:
                for key in missing_keys:
                    cache[key] = summaries.get(key)
        return {
            key: summary
            for key in group_keys
            if (summary := cache.get(key)) is not None
        }

    def request_activity_group_children(self, group_key: str) -> None:
        filter_key = self._activity_history_filter_key()
        if filter_key is None:
            return
        cache_key = (filter_key, group_key)
        if (
            cache_key in self._activity_group_child_events
            or cache_key in self._activity_group_child_load_pending
        ):
            return
        self._activity_group_child_load_pending.add(cache_key)
        GLib.idle_add(
            self._load_activity_group_children,
            filter_key,
            group_key,
        )

    def _load_activity_group_children(
        self,
        filter_key: tuple[str, str, str, tuple[str, ...]],
        group_key: str,
    ) -> bool:
        cache_key = (filter_key, group_key)
        profile_id, operation, history_state, patterns = filter_key
        scroll_state = self._capture_activity_scroll()
        try:
            if self._activity_store is None:
                self._activity_store = ActivityStore()
            events = self._activity_store.group_events(
                group_key,
                ACTIVITY_GROUP_CHILD_LIMIT,
                profile_id=profile_id or None,
                operation=operation or None,
                state=history_state or None,
                hidden_patterns=patterns,
            )
        except Exception:
            events = None
        finally:
            self._activity_group_child_load_pending.discard(cache_key)
        if events is None:
            return False
        self._activity_group_child_events[cache_key] = events
        if filter_key == self._activity_history_filter_key():
            structure_changed = self._render_filtered_activity()
            if structure_changed:
                self._schedule_activity_scroll_restore(scroll_state)
        return False

    def _render_filtered_activity(self) -> bool:
        events = self._filtered_activity_events()
        groups = group_activity_events(events)
        add_activity_group_headers(
            groups,
            self._recent_activity_group_headers(),
        )
        merge_activity_group_summaries(
            groups,
            self._complete_activity_group_summaries(groups),
        )
        current_rows = self._current_activity_group_rows()
        desired_keys = [str(group["group_key"]) for group in groups]
        current_keys = [row.activity_key for row in current_rows]
        if not groups:
            self._activity_render_generation += 1
            if self.activity_list.get_first_child() is None or current_rows:
                self._clear_listbox(self.activity_list)
                self._append_empty_activity(
                    self.activity_list, tr("sync_no_activity")
                )
                return True
            return False

        structure_changed = current_keys != desired_keys
        desired_set = set(desired_keys)
        child = self.activity_list.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            if (
                not isinstance(child, ActivityGroupRow)
                or child.activity_key not in desired_set
            ):
                self.activity_list.remove(child)
                structure_changed = True
            child = next_child

        self._activity_render_generation += 1
        generation = self._activity_render_generation
        self._activity_render_cursor = 0
        self._activity_render_keys = desired_keys
        self._activity_render_groups = {
            str(group["group_key"]): group for group in groups
        }
        GLib.idle_add(
            self._render_activity_group_batch,
            generation,
            priority=GLib.PRIORITY_DEFAULT_IDLE,
        )
        return structure_changed

    def _current_activity_group_rows(self) -> list[ActivityGroupRow]:
        rows: list[ActivityGroupRow] = []
        child = self.activity_list.get_first_child()
        while child is not None:
            if isinstance(child, ActivityGroupRow):
                rows.append(child)
            child = child.get_next_sibling()
        return rows

    def _render_activity_group_batch(self, generation: int) -> bool:
        if generation != self._activity_render_generation:
            return False

        rows = self._current_activity_group_rows()
        processed = 0
        while (
            self._activity_render_cursor < len(self._activity_render_keys)
            and processed < ACTIVITY_GROUP_RENDER_BATCH_SIZE
        ):
            index = self._activity_render_cursor
            key = self._activity_render_keys[index]
            group = self._activity_render_groups[key]
            if index < len(rows) and rows[index].activity_key == key:
                rows[index].update_group(group)
            else:
                row = next(
                    (
                        candidate
                        for candidate in rows[index:]
                        if candidate.activity_key == key
                    ),
                    None,
                )
                if row is not None:
                    self.activity_list.remove(row)
                    rows.remove(row)
                    row.update_group(group)
                else:
                    row = ActivityGroupRow(self, group)
                self.activity_list.insert(row, index)
                rows.insert(index, row)
            self._activity_render_cursor += 1
            processed += 1

        if self._activity_render_cursor >= len(self._activity_render_keys):
            return False
        return True

    def _capture_activity_scroll(self) -> dict[str, Any]:
        adjustment = self.activity_scrolled.get_vadjustment()
        value = adjustment.get_value()
        state: dict[str, Any] = {"value": value}
        if value <= 0:
            return state

        child = self.activity_list.get_first_child()
        while child is not None:
            if isinstance(child, ActivityGroupRow):
                allocation = child.get_allocation()
                if allocation.y + allocation.height > value:
                    state["anchor"] = child.activity_key
                    state["offset"] = value - allocation.y
                    break
            child = child.get_next_sibling()
        return state

    def _schedule_activity_scroll_restore(
        self,
        state: dict[str, Any],
    ) -> None:
        if float(state.get("value", 0) or 0) <= 0:
            return
        self._activity_scroll_restore_id += 1
        restore_id = self._activity_scroll_restore_id
        GLib.idle_add(
            self._restore_activity_scroll,
            restore_id,
            state,
            0,
            priority=GLib.PRIORITY_LOW,
        )

    def _restore_activity_scroll(
        self,
        restore_id: int,
        state: dict[str, Any],
        attempt: int,
    ) -> bool:
        if restore_id != self._activity_scroll_restore_id:
            return False

        adjustment = self.activity_scrolled.get_vadjustment()
        target = float(state.get("value", 0) or 0)
        anchor = state.get("anchor")
        anchor_found = False

        child = self.activity_list.get_first_child()
        while child is not None:
            if (
                isinstance(child, ActivityGroupRow)
                and child.activity_key == anchor
            ):
                allocation = child.get_allocation()
                if allocation.height > 0:
                    target = allocation.y + float(
                        state.get("offset", 0) or 0
                    )
                    anchor_found = True
                break
            child = child.get_next_sibling()

        maximum = max(
            adjustment.get_lower(),
            adjustment.get_upper() - adjustment.get_page_size(),
        )
        if attempt < 2 and (
            maximum <= adjustment.get_lower()
            or (anchor is not None and not anchor_found)
        ):
            GLib.timeout_add(
                20,
                self._restore_activity_scroll,
                restore_id,
                state,
                attempt + 1,
            )
            return False

        adjustment.set_value(
            min(max(target, adjustment.get_lower()), maximum)
        )
        return False

    def load_activity_thumbnail(
        self,
        image: Gtk.Image,
        item: dict[str, Any],
    ) -> None:
        local_path = str(item.get("local_path", "")).strip()
        if not local_path:
            image.set_from_icon_name("text-x-generic")
            return
        path = Path(local_path)
        content_type, _uncertain = Gio.content_type_guess(path.name, None)
        try:
            image.set_from_gicon(Gio.content_type_get_icon(content_type))
        except Exception:
            image.set_from_icon_name("text-x-generic")

        file = Gio.File.new_for_path(str(path))
        uri = file.get_uri()
        cached = self._cached_thumbnail_for_uri(uri)
        if cached:
            image.set_from_file(cached)
            image.set_pixel_size(56)
            return

        if self.thumbnail_factory is None:
            return

        self._activity_thumbnail_queue.append(
            (file, image, uri, content_type)
        )
        self._schedule_activity_thumbnail_pump()

    def _schedule_activity_thumbnail_pump(self) -> None:
        if self._activity_thumbnail_pump_scheduled:
            return
        self._activity_thumbnail_pump_scheduled = True
        GLib.idle_add(
            self._pump_activity_thumbnail_queue,
            priority=GLib.PRIORITY_LOW,
        )

    def _pump_activity_thumbnail_queue(self) -> bool:
        self._activity_thumbnail_pump_scheduled = False
        while (
            self._activity_thumbnail_queue
            and self._activity_thumbnail_queries_active
            < ACTIVITY_THUMBNAIL_QUERY_LIMIT
        ):
            file, image, uri, content_type = (
                self._activity_thumbnail_queue.popleft()
            )
            if image.get_root() is None:
                continue
            self._activity_thumbnail_queries_active += 1
            try:
                file.query_info_async(
                    "time::modified",
                    Gio.FileQueryInfoFlags.NONE,
                    GLib.PRIORITY_LOW,
                    None,
                    self._on_activity_thumbnail_info,
                    (image, uri, content_type),
                )
            except Exception:
                self._activity_thumbnail_queries_active -= 1
        return False

    def _on_activity_thumbnail_info(
        self,
        file: Gio.File,
        result: Gio.AsyncResult,
        user_data: tuple[Gtk.Image, str, str],
    ) -> None:
        image, uri, content_type = user_data
        try:
            info = file.query_info_finish(result)
            if image.get_root() is None:
                return
            mtime = int(info.get_attribute_uint64("time::modified"))
            cached = self.thumbnail_factory.lookup(uri, mtime)
            if cached:
                image.set_from_file(cached)
                image.set_pixel_size(56)
                return
            if not self.thumbnail_factory.can_thumbnail(uri, content_type, mtime):
                return
            if self.thumbnail_factory.has_valid_failed_thumbnail(uri, mtime):
                return
            self.thumbnail_factory.generate_thumbnail_async(
                uri,
                content_type,
                None,
                self._on_thumbnail_generated,
                (image, uri, mtime),
            )
        except Exception:
            pass
        finally:
            self._activity_thumbnail_queries_active = max(
                0,
                self._activity_thumbnail_queries_active - 1,
            )
            if self._activity_thumbnail_queue:
                self._schedule_activity_thumbnail_pump()

    def _cached_thumbnail_for_uri(self, uri: str) -> str | None:
        name = f"{hashlib.md5(uri.encode('utf-8')).hexdigest()}.png"
        for size in ("normal", "large", "x-large", "xx-large"):
            candidate = CACHE_DIR.parent / "thumbnails" / size / name
            if candidate.is_file():
                return str(candidate)
        return None

    def _on_thumbnail_generated(
        self,
        factory: Any,
        result: Gio.AsyncResult,
        user_data: tuple[Gtk.Image, str, int],
    ) -> None:
        image, uri, mtime = user_data
        try:
            pixbuf = factory.generate_thumbnail_finish(result)
            if pixbuf is None:
                return
            factory.save_thumbnail(pixbuf, uri, mtime, None)
            image.set_from_paintable(Gdk.Texture.new_for_pixbuf(pixbuf))
            image.set_pixel_size(56)
        except Exception:
            try:
                factory.create_failed_thumbnail(uri, mtime, None)
            except Exception:
                pass

    def show_activity_location(self, item: dict[str, Any]) -> None:
        path = str(item.get("reveal_path") or item.get("local_path", ""))
        if path:
            show_file_in_nautilus(path)

    def activity_location_available(self, item: dict[str, Any]) -> bool:
        profile_id = str(item.get("profile_id", "")).strip()
        path = str(item.get("reveal_path") or item.get("local_path", "")).strip()
        if not profile_id or not path:
            return False
        profile = list_profiles().get(profile_id)
        return bool(profile and is_profile_mounted(profile))

    def clear_activity_history(self) -> None:
        ActivityStore().clear_history()
        self._activity_visible_history_cache = None
        self._activity_history_cleared_at = time.time()
        self._activity_history_events = []
        self._activity_history_initialized = True
        self._activity_history_total = 0
        self._activity_history_global_offset = 0
        self._activity_history_profile_totals = {}
        self._activity_group_summary_cache.clear()
        self._activity_group_header_cache.clear()
        self._activity_group_child_events.clear()
        self._activity_group_child_load_pending.clear()
        self._activity_history_summary_signature = None
        self._activity_history_recent_group_signatures = {}
        self._activity_profile_summary_cache.clear()
        self._activity_page_profile_id = ""
        activity = self._without_activity_history(self._last_sync_activity)
        write_sync_snapshot(activity)
        self._refresh_sync_activity()

    @staticmethod
    def _without_activity_history(
        source: dict[str, Any],
    ) -> dict[str, Any]:
        activity = dict(source)
        active = list(activity.get("active", []) or [])
        queued = list(activity.get("queued", []) or [])
        activity["recent"] = []
        activity["events"] = [*active, *queued]
        activity["history_total"] = 0
        activity["history_summary"] = {
            "day": {
                "bytes": 0,
                "completed": 0,
                "errors": 0,
                "deleted": 0,
                "renamed": 0,
                "modified": 0,
            },
            "month": {
                "bytes": 0,
                "completed": 0,
                "errors": 0,
                "deleted": 0,
                "renamed": 0,
                "modified": 0,
            },
        }
        return activity

    def prioritize_activity(self, item: dict[str, Any]) -> None:
        try:
            queue_id = int(item.get("queue_id"))
        except (TypeError, ValueError):
            return
        profile_id = str(item.get("profile_id", ""))
        if not profile_id:
            return
        if not queue_set_expiry(profile_id, queue_id, -1_000_000_000):
            show_message(self, tr("error"), tr("upload_now_failed"))
            return
        GLib.timeout_add(250, self._refresh_once)

    def refresh(self, *, refresh_activity: bool = True) -> None:
        main_scroll_position = (
            self.main_scrolled.get_vadjustment().get_value()
        )
        all_profiles = list_profiles()
        known_profile_ids = set(all_profiles)
        self._pending_unmount.intersection_update(known_profile_ids)
        self._mounting_profiles.intersection_update(known_profile_ids)
        focus = self.get_focus()
        if (
            self._widget_is_descendant(focus, self.mounted_box)
            or self._widget_is_descendant(focus, self.unmounted_box)
        ):
            self.set_focus(None)

        if refresh_activity:
            self._refresh_sync_activity()
        self._clear_flowbox(self.mounted_box)
        self._clear_flowbox(self.unmounted_box)

        show_hidden = bool(
            self.settings.get("show_hidden_profiles", False)
        )
        profiles = {
            profile_id: profile
            for profile_id, profile in all_profiles.items()
            if show_hidden or not bool(profile.get("hidden", False))
        }
        self.empty_label.set_text(
            (
                tr("no_visible_profiles")
                if all_profiles and not profiles
                else tr("no_profiles")
            )
        )
        self.empty_label.set_visible(not bool(profiles))
        has_mounted = False
        has_unmounted = False

        profile_states = [
            (profile_id, profile, is_profile_mounted(profile))
            for profile_id, profile in sorted(
                profiles.items(),
                key=lambda item: str(
                    item[1].get("label", item[0])
                ).lower(),
            )
        ]
        unmounted_count = sum(
            1 for _profile_id, _profile, mounted in profile_states
            if not mounted
        )
        self.unmounted_box.set_max_children_per_line(
            unmounted_count if unmounted_count in (2, 3) else 4
        )

        for profile_id, profile, mounted in profile_states:
            if mounted:
                self._cleared_unmounted_icons.discard(profile_id)
            elif profile_id not in self._cleared_unmounted_icons:
                clear_mount_folder_icon(profile)
                self._cleared_unmounted_icons.add(profile_id)

            sync_state = self._last_sync_activity.get("profiles", {}).get(
                profile_id,
                {},
            )
            card = ProfileCard(
                self,
                profile_id,
                profile,
                mounted,
                sync_state,
            )
            if mounted:
                has_mounted = True
                self.mounted_box.append(card)
            else:
                has_unmounted = True
                self.unmounted_box.append(card)

        self.mounted_section.set_visible(has_mounted)
        self.unmounted_label.set_visible(has_unmounted)
        self.unmounted_box.set_visible(has_unmounted)
        self._schedule_main_scroll_restore(main_scroll_position)

    @staticmethod
    def _widget_is_descendant(
        widget: Gtk.Widget | None,
        ancestor: Gtk.Widget,
    ) -> bool:
        while widget is not None:
            if widget is ancestor:
                return True
            widget = widget.get_parent()
        return False

    def _schedule_main_scroll_restore(self, value: float) -> None:
        self._main_scroll_restore_id += 1
        restore_id = self._main_scroll_restore_id
        GLib.idle_add(
            self._restore_main_scroll,
            restore_id,
            value,
            0,
            priority=GLib.PRIORITY_LOW,
        )

    def _restore_main_scroll(
        self,
        restore_id: int,
        value: float,
        attempt: int,
    ) -> bool:
        if restore_id != self._main_scroll_restore_id:
            return False

        adjustment = self.main_scrolled.get_vadjustment()
        maximum = max(
            adjustment.get_lower(),
            adjustment.get_upper() - adjustment.get_page_size(),
        )
        geometry_not_ready = (
            value > adjustment.get_lower()
            and maximum <= adjustment.get_lower()
        )
        if attempt < 2 and geometry_not_ready:
            GLib.timeout_add(
                20,
                self._restore_main_scroll,
                restore_id,
                value,
                attempt + 1,
            )
            return False

        adjustment.set_value(
            min(max(value, adjustment.get_lower()), maximum)
        )
        if attempt == 0:
            GLib.timeout_add(
                20,
                self._restore_main_scroll,
                restore_id,
                value,
                attempt + 1,
            )
        return False

    def edit_profile(self, profile_id: str) -> None:
        profiles = list_profiles()
        profile = profiles.get(profile_id)

        if not profile:
            return

        ProfileDialog(
            self,
            self._after_profile_saved,
            profile_id=profile_id,
            profile=profile,
        ).present(self)

    def _after_profile_saved(self) -> None:
        self.refresh()

    def show_settings(self, _button: Gtk.Button | None = None) -> None:
        SettingsDialog(self, self.settings, self._after_settings_saved).present(self)

    def show_about(self, _button: Gtk.Button | None = None) -> None:
        version = installed_rclone_version()
        rclone_version_text = (
            ".".join(str(part) for part in version)
            if version is not None
            else tr("about_rclone_unavailable")
        )
        create_application_about_dialog(rclone_version_text).present(self)

    def schedule_sponsor_reminder(self) -> None:
        if (
            self._sponsor_reminder_source
            or self._sponsor_reminder_open
            or self._sponsor_reminder_shown
        ):
            return
        state = load_state()
        if bool(state.get("sponsor_reminder_disabled", False)):
            self._sponsor_reminder_shown = True
            return
        now = time.time()
        first_used_at = _valid_timestamp(
            state.get("sponsor_first_used_at")
        )
        if not first_used_at:
            first_used_at = now
            remind_after = now + SPONSOR_INITIAL_DELAY_SECONDS
            update_sponsor_reminder_state(
                first_used_at=first_used_at,
                remind_after=remind_after,
                disabled=False,
            )
            state = {
                **state,
                "sponsor_first_used_at": first_used_at,
                "sponsor_remind_after": remind_after,
                "sponsor_reminder_disabled": False,
            }
        due_at = _sponsor_reminder_due_at(state)
        delay = due_at - now
        if delay <= 0:
            self._sponsor_reminder_source = GLib.timeout_add(
                1200,
                self._show_sponsor_reminder,
            )
        else:
            self._sponsor_reminder_source = GLib.timeout_add_seconds(
                max(1, int(delay)),
                self._show_sponsor_reminder,
            )

    def _show_sponsor_reminder(self) -> bool:
        self._sponsor_reminder_source = 0
        state = load_state()
        if not _sponsor_reminder_due(state, time.time()):
            if not bool(state.get("sponsor_reminder_disabled", False)):
                self.schedule_sponsor_reminder()
            return GLib.SOURCE_REMOVE
        self._sponsor_reminder_shown = True
        self._sponsor_reminder_open = True
        dialog = Adw.AlertDialog.new(
            tr("sponsor_reminder_title"),
            tr("about_support_body"),
        )
        extra = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
        )
        sponsor_card = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
        )
        sponsor_card.add_css_class("card")
        sponsor_row, sponsor_button = _create_sponsor_action_row()
        sponsor_button.connect(
            "clicked",
            self._on_sponsor_reminder_button_clicked,
            dialog,
        )
        sponsor_card.append(sponsor_row)
        extra.append(sponsor_card)
        never_show = Gtk.CheckButton(
            label=tr("sponsor_reminder_never")
        )
        never_show.set_halign(Gtk.Align.START)
        extra.append(never_show)
        dialog.set_extra_child(extra)
        dialog.add_response("later", tr("sponsor_reminder_later"))
        dialog.add_response("close", tr("close"))
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        never_show.connect(
            "toggled",
            self._on_sponsor_never_show_toggled,
            dialog,
        )
        dialog.connect(
            "response",
            self._on_sponsor_reminder_response,
            never_show,
        )
        dialog.present(self)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _on_sponsor_never_show_toggled(
        checkbox: Gtk.CheckButton,
        dialog: Adw.AlertDialog,
    ) -> None:
        dialog.set_response_enabled(
            "later",
            not checkbox.get_active(),
        )

    def _on_sponsor_reminder_button_clicked(
        self,
        _button: Gtk.Button,
        dialog: Adw.AlertDialog,
    ) -> None:
        update_sponsor_reminder_state(
            remind_after=time.time() + SPONSOR_AFTER_VISIT_SECONDS,
        )
        _open_sponsor_page(None)
        dialog.close()

    def _on_sponsor_reminder_response(
        self,
        _dialog: Adw.AlertDialog,
        response: str,
        never_show: Gtk.CheckButton,
    ) -> None:
        self._sponsor_reminder_open = False
        now = time.time()
        if never_show.get_active():
            update_sponsor_reminder_state(disabled=True)
        elif response == "later":
            update_sponsor_reminder_state(
                remind_after=now + SPONSOR_REMIND_LATER_SECONDS,
            )

    def show_remote_manager(
        self,
        _button: Gtk.Button | None = None,
    ) -> None:
        RemoteManagerDialog(
            self,
            self.refresh,
        ).present(self)

    def _after_settings_saved(self, settings: dict[str, Any]) -> None:
        activity_patterns_changed = (
            normalize_activity_patterns(self.settings.get("activity_hidden_patterns"))
            != normalize_activity_patterns(settings.get("activity_hidden_patterns"))
        )
        language_changed = (
            str(self.settings.get("language", "system"))
            != str(settings.get("language", "system"))
        )
        limits_changed = any(
            str(self.settings.get(key, "")) != str(settings.get(key, ""))
            for key in ("upload_speed_limit", "download_speed_limit")
        )
        self.settings = dict(settings)
        if activity_patterns_changed:
            self._on_activity_visibility_changed()
        _apply_color_scheme(str(settings.get("color_scheme", "system")))
        self._ensure_tray_running()
        self.refresh()
        if limits_changed:
            results = apply_bwlimit_to_running_mounts(list_profiles(), settings)
            failed = [profile_id for profile_id, ok in results.items() if not ok]
            message = (
                tr("speed_limits_apply_failed", profiles=", ".join(failed))
                if failed
                else tr("speed_limits_applied")
            )
            if language_changed:
                message = f"{message}\n\n{tr('settings_language_restart')}"
            show_message(
                self,
                tr("error") if failed else tr("info"),
                message,
            )
        elif language_changed:
            show_message(
                self,
                tr("info"),
                tr("settings_language_restart"),
            )

    def mount_profile(
        self,
        profile_id: str,
        open_after_mount: bool | None = None,
    ) -> None:
        if profile_id in self._mounting_profiles:
            return
        profiles = list_profiles()
        profile = profiles.get(profile_id)
        if not profile:
            return

        target_conflict = find_profile_target_conflict(
            profile_id,
            str(profile.get("remote_spec", "")),
            str(profile.get("mount_dir", "")),
        )
        if target_conflict:
            show_message(self, tr("error"), target_conflict)
            return

        should_open = (
            bool(self.settings.get("open_after_mount"))
            if open_after_mount is None
            else open_after_mount
        )
        self._mounting_profiles.add(profile_id)
        self.refresh()
        GLib.timeout_add(
            50,
            self._start_mount_profile,
            profile_id,
            should_open,
        )

    def _start_mount_profile(
        self,
        profile_id: str,
        open_after_mount: bool,
    ) -> bool:
        if not systemd_start(profile_id):
            self._mounting_profiles.discard(profile_id)
            self.refresh()
            show_message(self, tr("error"), tr("mount_failed", profile=profile_id))
            return False
        set_profile_pending_unmount(profile_id, False)

        GLib.timeout_add(
            MOUNT_CHECK_INITIAL_DELAY_MS,
            self._check_mount_result,
            profile_id,
            open_after_mount,
            1,
        )
        return False

    def _check_mount_result(
        self,
        profile_id: str,
        open_after_mount: bool,
        attempt: int,
    ) -> bool:
        profiles = list_profiles()
        profile = profiles.get(profile_id)

        if profile and is_profile_mounted(profile):
            self._mounting_profiles.discard(profile_id)
            set_mount_folder_icon(profile)
            set_profile_desired_mounted(profile_id, True)
            if open_after_mount:
                open_mount_dir(profile_id)
        elif profile and attempt < MOUNT_CHECK_MAX_ATTEMPTS:
            GLib.timeout_add(
                MOUNT_CHECK_RETRY_DELAY_MS,
                self._check_mount_result,
                profile_id,
                open_after_mount,
                attempt + 1,
            )
            return False
        elif profile:
            self._mounting_profiles.discard(profile_id)
            log_text = read_tail(log_file_for(profile_id))
            if not log_text.strip():
                log_text = tr(
                    "mount_failed_no_log",
                    profile=profile_id,
                    command=f"journalctl --user -u talaryn@{profile_id}.service --no-pager -n 100",
                )

            show_text_window(
                self,
                tr("mount_failed", profile=profile_id),
                log_text,
            )
        else:
            self._mounting_profiles.discard(profile_id)

        self.refresh()
        return False

    def request_unmount_profile(self, profile_id: str) -> None:
        activity = get_sync_activity()
        state = activity.get("profiles", {}).get(profile_id, {})
        if snapshot_allows_unmount(activity, profile_id):
            self.unmount_profile(profile_id)
            return

        dialog = Adw.AlertDialog.new(
            tr("unmount_during_sync_title") if state.get("transfer_state") == "busy" else tr("unmount"),
            tr("unmount_safety_unknown") if state.get("transfer_state") != "busy" else tr(
                "unmount_during_sync_body",
                active=int(state.get("active_count", 0)),
                queued=int(state.get("queued_count", 0)),
            ),
        )
        dialog.add_response("cancel", tr("cancel"))
        dialog.add_response("wait", tr("wait_and_unmount"))
        dialog.add_response("unmount", tr("unmount_anyway"))
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance(
            "wait",
            Adw.ResponseAppearance.SUGGESTED,
        )
        dialog.set_response_appearance(
            "unmount",
            Adw.ResponseAppearance.DESTRUCTIVE,
        )
        dialog.connect(
            "response",
            self._on_unmount_warning_response,
            profile_id,
        )
        dialog.present(self)

    def _on_unmount_warning_response(
        self,
        _dialog: Adw.AlertDialog,
        response: str,
        profile_id: str,
    ) -> None:
        if response == "wait":
            set_profile_desired_mounted(profile_id, False)
            set_profile_pending_unmount(profile_id, True)
            self._pending_unmount.add(profile_id)
            show_message(self, tr("info"), tr("waiting_to_unmount"))
        elif response == "unmount":
            self.unmount_profile(profile_id, force=True)

    def unmount_profile(self, profile_id: str, *, force: bool = False) -> None:
        if not force and probe_transfer_state(profile_id) != "idle":
            show_message(self, tr("info"), tr("unmount_safety_blocked", profile=profile_id))
            return
        self._pending_unmount.discard(profile_id)
        set_profile_pending_unmount(profile_id, False)
        set_profile_desired_mounted(profile_id, False)
        profile = list_profiles().get(profile_id)
        if profile is None:
            return
        if not systemd_stop(profile_id):
            unmount_raw(profile_id)
        clear_mount_folder_icon(profile)
        GLib.timeout_add(900, self._refresh_once)

    def set_profile_hidden(
        self,
        profile_id: str,
        hidden: bool,
    ) -> None:
        profile = list_profiles().get(profile_id)
        if profile is None:
            return
        updated = dict(profile)
        if hidden:
            updated["hidden"] = True
        else:
            updated.pop("hidden", None)
        save_profile(profile_id, updated)
        self.refresh()

    def show_log(self, profile_id: str) -> None:
        text = read_tail(log_file_for(profile_id))
        if not text.strip():
            text = tr("no_log")
        show_text_window(self, f"{tr('show_log')} — {profile_id}", text)

    def clear_profile_logs(self, profile_id: str) -> None:
        if clear_logs_for(profile_id):
            show_message(
                self,
                tr("info"),
                tr("logs_cleared", profile=profile_id),
            )
        else:
            show_message(
                self,
                tr("error"),
                tr("logs_clear_failed", profile=profile_id),
            )

    def show_properties(self, profile_id: str) -> None:
        profiles = list_profiles()
        profile = profiles.get(profile_id)

        if not profile:
            return

        mount_dir = expand_path(str(profile.get("mount_dir", "")))
        mounted = is_profile_mounted(profile)

        try:
            cmd = build_mount_command(profile_id, profile)
            current_command = format_command_for_display(cmd)
        except Exception as exc:
            current_command = f"{tr('error')}: {exc}"

        last_command_log = read_tail(command_file_for(profile_id), max_chars=6000)
        if not last_command_log.strip():
            last_command_log = tr("no_command_log")

        PropertiesDialog(
            profile_id,
            profile,
            mount_dir,
            mounted,
            current_command,
            last_command_log,
            disk_usage_for_path(mount_dir),
        ).present(self)

    def open_profile(self, profile_id: str) -> None:
        open_mount_dir(profile_id)

    def _refresh_once(self) -> bool:
        self.refresh()
        return False


class TalarynApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS
        )
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", self._on_quit_action)
        self.add_action(quit_action)

    def _on_quit_action(
        self,
        _action: Gio.SimpleAction,
        _parameter: GLib.Variant | None,
    ) -> None:
        main_window = next(
            (
                window
                for window in self.get_windows()
                if isinstance(window, MainWindow)
            ),
            None,
        )
        if (
            main_window is not None
            and main_window.confirm_close_if_needed(self._quit_now)
        ):
            return
        self._quit_now()

    def _quit_now(self) -> None:
        for window in list(self.get_windows()):
            if isinstance(window, MainWindow):
                window.force_close = True
            window.close()
        self.quit()

    def do_activate(self) -> None:
        window = next(
            (
                candidate
                for candidate in self.get_windows()
                if isinstance(candidate, MainWindow)
            ),
            None,
        )
        if window is None:
            window = MainWindow(self)
        else:
            window.refresh()
        window.present()
        window.schedule_sponsor_reminder()


def show_message(parent: Gtk.Window, title: str, text: str) -> None:
    dialog = Adw.AlertDialog.new(title, text)
    dialog.add_response("ok", "OK")
    dialog.set_default_response("ok")
    dialog.set_close_response("ok")
    dialog.present(parent)


def show_text_window(parent: Gtk.Window, title: str, text: str) -> None:
    window = TextDialog(title, text)
    application = parent.get_application()
    if application is not None:
        window.set_application(application)
    window.set_transient_for(parent)
    window.set_destroy_with_parent(True)
    window.present()


def run() -> int:
    app = TalarynApplication()
    return app.run(None)
