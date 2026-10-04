from __future__ import annotations

import sys
import json

from .config import (
    delete_profile,
    get_profile,
    list_profiles,
    load_settings,
    set_profile_desired_mounted,
    set_profile_pending_unmount,
)
from .i18n import apply_toolkit_language, tr
from .mount import clear_mount_folder_icon, is_profile_mounted, mount_foreground, open_mount_dir, unmount_raw
from .paths import ensure_runtime_dirs
from .rc import MIN_RCLONE_VERSION, installed_rclone_version, version_is_supported
from .systemd import start as systemd_start
from .systemd import stop as systemd_stop
from .systemd import is_active as systemd_is_active
from .transfer_safety import probe_transfer_state
from .util import notify


def print_usage() -> None:
    print(
        """Usage:
  talaryn gui
  talaryn tray
  talaryn list
  talaryn status PROFILE
  talaryn mount PROFILE
  talaryn unmount PROFILE [--force]
  talaryn open PROFILE
  talaryn forget PROFILE
  talaryn mount-foreground PROFILE
  talaryn unmount-raw PROFILE
  talaryn google-export PROFILE LOCAL_LINK_PATH
  talaryn google-create PROFILE LOCAL_FOLDER KIND [WINDOW_CONTEXT_JSON]
  talaryn compare [LOCAL_FILE_PATH ...]
  talaryn monitor
"""
    )


def main(argv: list[str] | None = None) -> None:
    ensure_runtime_dirs()
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "gui"
    version = installed_rclone_version()
    if not version_is_supported(version):
        required = ".".join(str(value) for value in MIN_RCLONE_VERSION)
        found = ".".join(str(value) for value in version) if version else "unknown"
        print(
            tr("rclone_version_required", required=required, found=found),
            file=sys.stderr,
        )
        raise SystemExit(2)

    if cmd == "gui":
        apply_toolkit_language()
        from .app import run

        raise SystemExit(run())

    if cmd == "tray":
        apply_toolkit_language()
        from .tray import run

        raise SystemExit(run())

    if cmd == "monitor":
        from .monitor import run_monitor

        raise SystemExit(run_monitor())

    if cmd == "google-export":
        if len(args) != 3:
            print_usage()
            raise SystemExit(1)
        profile_id = args[1]
        try:
            profile = get_profile(profile_id)
        except KeyError:
            print(f"Missing profile: {profile_id}", file=sys.stderr)
            raise SystemExit(1)
        apply_toolkit_language()
        from .google_export import run_google_export

        raise SystemExit(run_google_export(profile, args[2]))

    if cmd == "google-create":
        if len(args) not in (4, 5):
            print_usage()
            raise SystemExit(1)
        from .google_create import KINDS, relative_folder
        try:
            profile = get_profile(args[1])
            relative_folder(profile, args[2])
            if args[3] not in KINDS:
                raise ValueError("Invalid Google document kind")
            context = json.loads(args[4]) if len(args) == 5 else {}
            if not isinstance(context, dict):
                raise ValueError("Invalid window context")
        except (KeyError, ValueError) as error:
            print(str(error), file=sys.stderr)
            raise SystemExit(1)
        apply_toolkit_language()
        from .google_create_dialog import run_google_create
        raise SystemExit(run_google_create(args[1], profile, args[2], args[3], context))

    if cmd == "compare":
        if not load_settings().get("context_file_comparison", False):
            message = tr("comparison_disabled")
            print(message, file=sys.stderr)
            notify(tr("comparison_title"), message)
            raise SystemExit(2)
        apply_toolkit_language()
        from .comparison_dialog import run_file_comparison

        raise SystemExit(run_file_comparison(args[1:]))

    if cmd == "list":
        for profile_id, profile in sorted(list_profiles().items()):
            state = "mounted" if is_profile_mounted(profile) else "unmounted"
            print(f"{profile_id}\t{state}\t{profile.get('remote_spec', '')}\t{profile.get('mount_dir', '')}")
        return

    if len(args) < 2:
        print_usage()
        raise SystemExit(1)

    profile_id = args[1]

    try:
        profile = get_profile(profile_id)
    except KeyError:
        print(f"Missing profile: {profile_id}", file=sys.stderr)
        raise SystemExit(1)

    if cmd == "status":
        mounted = is_profile_mounted(profile)
        print("mounted" if mounted else "unmounted")
        raise SystemExit(0 if mounted else 1)

    if cmd == "mount":
        ok = systemd_start(profile_id)
        if ok:
            set_profile_pending_unmount(profile_id, False)
            set_profile_desired_mounted(profile_id, True)
        notify(tr("app_title"), tr("mounted" if ok else "mount_failed", profile=profile_id))
        raise SystemExit(0 if ok else 1)

    if cmd == "unmount":
        if args[2:] not in ([], ["--force"]):
            print_usage()
            raise SystemExit(1)
        if "--force" not in args[2:] and probe_transfer_state(profile_id) != "idle":
            print(tr("unmount_safety_blocked", profile=profile_id), file=sys.stderr)
            print(tr("unmount_force_hint"), file=sys.stderr)
            raise SystemExit(2)
        set_profile_pending_unmount(profile_id, False)
        set_profile_desired_mounted(profile_id, False)
        ok = systemd_stop(profile_id) or unmount_raw(profile_id)
        clear_mount_folder_icon(profile)
        notify(tr("app_title"), tr("unmounted" if ok else "unmount_failed", profile=profile_id, path=profile.get("mount_dir", "")))
        raise SystemExit(0 if ok else 1)

    if cmd == "open":
        open_mount_dir(profile_id)
        return

    if cmd == "forget":
        if is_profile_mounted(profile) or systemd_is_active(profile_id):
            print(tr("forget_mounted_blocked", profile=profile_id), file=sys.stderr)
            raise SystemExit(2)
        set_profile_pending_unmount(profile_id, False)
        set_profile_desired_mounted(profile_id, False)
        delete_profile(profile_id)
        return

    if cmd == "mount-foreground":
        mount_foreground(profile_id)
        return

    if cmd == "unmount-raw":
        ok = unmount_raw(profile_id)
        raise SystemExit(0 if ok else 1)

    print_usage()
    raise SystemExit(1)
