#!/usr/bin/env python3
#
# Copyright (c) 2026 Kamil Krakowski
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

# Isolated workers also load the helper from this installed extension directory.
_extension_dir = str(Path(__file__).resolve().parent)
if _extension_dir not in sys.path:
    sys.path.insert(0, _extension_dir)
from talaryn_i18n import tr


import gi

gi.require_version("Nautilus", "4.0")

from gi.repository import GObject, Nautilus


DEFAULT_TERMINAL_COMMAND = "gnome-terminal -- bash -lc {ssh_command}"
SFTP_CONNECTION_FIELDS = frozenset(
    ("type", "host", "user", "port", "key_file", "ssh")
)
SFTP_CONFIG_FILTER = (
    "/^[[:space:]]*(type|host|user|port|key_file|ssh)"
    "[[:space:]]*=/ { print }"
)
RCLONE_DIAGNOSTIC_ENV = frozenset(
    (
        "RCLONE_DUMP",
        "RCLONE_DUMP_BODIES",
        "RCLONE_DUMP_HEADERS",
        "RCLONE_LOG_LEVEL",
        "RCLONE_USE_JSON_LOG",
        "RCLONE_VERBOSE",
    )
)


def _config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "talaryn"


def _absolute_path(value: str) -> Path:
    return Path(os.path.abspath(os.path.expandvars(os.path.expanduser(value))))


def _load_profiles() -> dict[str, dict]:
    try:
        data = json.loads((_config_dir() / "profiles.json").read_text(encoding="utf-8"))
    except Exception:
        return {}

    profiles = data.get("profiles", {}) if isinstance(data, dict) else {}
    return {str(k): v for k, v in profiles.items() if isinstance(v, dict)}


def _load_settings() -> dict:
    try:
        data = json.loads((_config_dir() / "settings.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _setting_enabled(key: str, default: bool = True) -> bool:
    return bool(_load_settings().get(key, default))


def _parse_sftp_connection(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        key = key.strip()
        if separator and key in SFTP_CONNECTION_FIELDS:
            result[key] = value.strip()
    return result


def _terminate_process(process) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _safe_rclone_environment() -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in os.environ.items():
        normalized = key.upper()
        if (
            normalized in RCLONE_DIAGNOSTIC_ENV
            or normalized.startswith("RCLONE_DUMP_")
            or normalized.startswith("RCLONE_LOG_FILE")
        ):
            continue
        result[key] = value
    return result


def _rclone_config(remote_name: str) -> dict[str, str]:
    remote_name = remote_name.strip().rstrip(":")
    if (
        not remote_name
        or remote_name.startswith("-")
        or any(char in remote_name for char in (":", "/", "\\"))
        or any(ord(char) < 32 or ord(char) == 127 for char in remote_name)
    ):
        return {}
    try:
        producer = subprocess.Popen(
            ["rclone", "config", "show", remote_name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_safe_rclone_environment(),
        )
    except OSError:
        return {}

    if producer.stdout is None:
        _terminate_process(producer)
        return {}

    try:
        filter_process = subprocess.Popen(
            ["awk", SFTP_CONFIG_FILTER],
            stdin=producer.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        producer.stdout.close()
        _terminate_process(producer)
        return {}

    producer.stdout.close()
    try:
        filtered, _stderr = filter_process.communicate(timeout=5)
        producer_returncode = producer.wait(timeout=1)
    except subprocess.TimeoutExpired:
        _terminate_process(filter_process)
        _terminate_process(producer)
        return {}

    if filter_process.returncode != 0 or producer_returncode != 0:
        return {}
    return _parse_sftp_connection(filtered)


def _notify(title: str, body: str) -> None:
    try:
        subprocess.run(
            ["notify-send", title, body],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


class RcloneSshTerminalProvider(GObject.GObject, Nautilus.MenuProvider):
    def get_background_items(self, current_folder):
        folder_path = self._file_to_path(current_folder)
        if not folder_path:
            return []
        return self._items_for_path(folder_path, is_directory=True)

    def get_file_items(self, files):
        if len(files) != 1:
            return []

        selected_path = self._file_to_path(files[0])
        if not selected_path:
            return []
        return self._items_for_path(selected_path, is_directory=files[0].is_directory())

    def _items_for_path(self, path: Path, is_directory: bool):
        match = self._profile_for_path(path)
        if not match:
            return []

        profile_id, profile, mount_dir = match
        items = []

        if is_directory and _setting_enabled("context_open_external_terminal"):
            item = Nautilus.MenuItem(
                name=f"RcloneSshTerminalProvider::{profile_id}::terminal",
                label=tr("context_open_external_terminal"),
                tip=tr("nautilus_terminal_help"),
            )
            item.connect("activate", self._open_terminal, path, profile, mount_dir)
            items.append(item)

        if _setting_enabled("context_copy_remote_path"):
            item = Nautilus.MenuItem(
                name=f"RcloneSshTerminalProvider::{profile_id}::copy-path",
                label=tr("context_copy_remote_path"),
                tip=tr("nautilus_copy_path_help"),
            )
            item.connect("activate", self._copy_remote_path, path, profile, mount_dir)
            items.append(item)

        return items

    def _file_to_path(self, file):
        location = file.get_location()
        if location is None:
            return None

        path = location.get_path()
        if path is None:
            return None

        return _absolute_path(path)

    def _profile_for_path(self, folder_path: Path):
        for profile_id, profile in _load_profiles().items():
            if str(profile.get("kind", "")) != "sftp":
                continue

            mount_dir = str(profile.get("mount_dir", "")).strip()
            if not mount_dir:
                continue

            mount_path = _absolute_path(mount_dir)
            try:
                folder_path.relative_to(mount_path)
            except ValueError:
                continue
            return profile_id, profile, mount_path

        return None

    def _remote_dir_for_path(self, folder_path: Path, profile: dict, mount_dir: Path) -> str:
        base = str(profile.get("remote_path", "")).strip()
        rel = folder_path.relative_to(mount_dir).as_posix()

        if rel == ".":
            rel = ""

        if not base and not rel:
            return "."

        if not base:
            return rel

        if not rel:
            return base

        if base.endswith("/"):
            return f"{base}{rel}"

        return f"{base}/{rel}"

    def _remote_spec_for_path(self, path: Path, profile: dict, mount_dir: Path) -> str:
        remote_name = str(profile.get("remote_name", "")).strip().rstrip(":")
        if not remote_name:
            remote_name = str(profile.get("remote_spec", "")).split(":", 1)[0].strip()

        remote_path = self._remote_dir_for_path(path, profile, mount_dir)
        if remote_path in ("", "."):
            return f"{remote_name}:"
        return f"{remote_name}:{remote_path}"

    def _ssh_destination(self, profile: dict):
        remote_name = str(profile.get("remote_name", "")).strip().rstrip(":")
        if not remote_name:
            remote_name = str(profile.get("remote_spec", "")).split(":", 1)[0].strip()

        cfg = _rclone_config(remote_name)
        if str(cfg.get("type", "")) != "sftp":
            return None

        ssh_command = str(cfg.get("ssh", "")).strip()
        if ssh_command:
            args = shlex.split(ssh_command)
            if args:
                return self._with_tty(args)

        host = str(cfg.get("host", "")).strip()
        if not host:
            return None

        user = str(cfg.get("user", "")).strip()
        port = str(cfg.get("port", "")).strip()
        key_file = str(cfg.get("key_file", "")).strip()

        args = ["ssh", "-t"]
        if port:
            args += ["-p", port]
        if key_file:
            args += ["-i", os.path.expanduser(key_file)]
        args.append(f"{user}@{host}" if user else host)
        return self._with_tty(args)

    def _with_tty(self, args: list[str]) -> list[str]:
        if not args or Path(args[0]).name != "ssh":
            return args

        if any(arg in ("-t", "-tt") for arg in args[1:]):
            return args

        return [args[0], "-t", *args[1:]]

    def _open_terminal(self, menu_item, folder_path: Path, profile: dict, mount_dir: Path):
        ssh_args = self._ssh_destination(profile)
        if not ssh_args:
            _notify(tr("ssh_open_failed"), tr("ssh_config_missing"))
            return

        remote_dir = self._remote_dir_for_path(folder_path, profile, mount_dir)
        ssh_args.append(f"cd {shlex.quote(remote_dir)} || exit $?; exec \"${{SHELL:-sh}}\"")
        error_format = "\n" + tr("ssh_exit_error", code="%s") + "\n"
        ssh_command = (
            f"{shlex.join(ssh_args)}; "
            "status=$?; "
            "if [ \"$status\" -ne 0 ]; then "
            f"printf {shlex.quote(error_format)} \"$status\"; "
            "read _; "
            "fi"
        )

        terminal_command = str(
            _load_settings().get("terminal_command", DEFAULT_TERMINAL_COMMAND)
        ).strip() or DEFAULT_TERMINAL_COMMAND

        if "{ssh_command}" not in terminal_command:
            terminal_command = f"{terminal_command} {{ssh_command}}"

        command_line = terminal_command.format(ssh_command=shlex.quote(ssh_command))
        try:
            subprocess.Popen(
                shlex.split(command_line),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            _notify(tr("terminal_open_failed"), str(exc))

    def _copy_remote_path(self, menu_item, path: Path, profile: dict, mount_dir: Path):
        remote_spec = self._remote_spec_for_path(path, profile, mount_dir)
        commands = (
            ["wl-copy"],
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        )

        for command in commands:
            try:
                result = subprocess.run(
                    command,
                    input=remote_spec,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=5,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue

            if result.returncode == 0:
                _notify(tr("remote_path_copied"), remote_spec)
                return

        _notify(
            tr("remote_path_copy_failed"),
            tr("clipboard_tool_missing"),
        )
