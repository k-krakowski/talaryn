from __future__ import annotations

import os
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from talaryn.mount import (
    _mount_exec_environment,
    build_mount_command,
    clear_logs_for,
    configured_bwlimit,
    format_command_for_log,
    show_file_in_nautilus,
)


class MountCommandTests(unittest.TestCase):
    def test_mount_environment_removes_rc_listener_overrides(self) -> None:
        with patch.dict(
            os.environ,
            {
                "RCLONE_RC": "true",
                "RCLONE_RC_ADDR": ":5572",
                "RCLONE_RC_NO_AUTH": "true",
                "RCLONE_CONFIG": "/tmp/rclone.conf",
                "RCLONE_GUI_TEST_VALUE": "kept",
            },
            clear=True,
        ):
            environment = _mount_exec_environment()

        self.assertNotIn("RCLONE_RC", environment)
        self.assertNotIn("RCLONE_RC_ADDR", environment)
        self.assertNotIn("RCLONE_RC_NO_AUTH", environment)
        self.assertEqual(environment["RCLONE_CONFIG"], "/tmp/rclone.conf")
        self.assertEqual(environment["RCLONE_GUI_TEST_VALUE"], "kept")

    def test_bwlimit_keeps_rclone_upload_download_order(self) -> None:
        self.assertEqual(
            configured_bwlimit(
                {
                    "upload_speed_limit": "10M",
                    "download_speed_limit": "2M",
                }
            ),
            "10M:2M",
        )
        self.assertEqual(
            configured_bwlimit(
                {
                    "upload_speed_limit": "",
                    "download_speed_limit": "5M",
                }
            ),
            "off:5M",
        )

    @patch(
        "talaryn.mount.load_settings",
        return_value={
            "upload_speed_limit": "8M",
            "download_speed_limit": "3M",
        },
    )
    def test_mount_command_adds_monitoring_and_speed_limit(
        self,
        _load_settings: object,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile = {
                "remote_spec": "remote:",
                "mount_dir": str(root / "mount"),
            }
            with (
                patch("talaryn.mount.ensure_runtime_dirs"),
                patch(
                    "talaryn.mount.cache_dir_for",
                    return_value=root / "cache",
                ),
                patch(
                    "talaryn.mount.log_file_for",
                    return_value=root / "example.log",
                ),
                patch(
                    "talaryn.mount.rc_socket_for",
                    return_value=root / "example.sock",
                ),
            ):
                command = build_mount_command("example", profile)

        self.assertIn("--rc", command)
        self.assertEqual(
            command[command.index("--rc-addr") + 1],
            f"unix://{root / 'example.sock'}",
        )
        self.assertIn("--bwlimit", command)
        self.assertEqual(command[command.index("--bwlimit") + 1], "8M:3M")
        self.assertEqual(
            command[command.index("--log-file-max-size") + 1],
            "10M",
        )
        self.assertEqual(
            command[command.index("--log-file-max-backups") + 1],
            "3",
        )

    @patch(
        "talaryn.mount.load_settings",
        return_value={
            "upload_speed_limit": "8M",
            "download_speed_limit": "3M",
        },
    )
    def test_custom_bwlimit_is_not_overridden(
        self,
        _load_settings: object,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile = {
                "remote_spec": "remote:",
                "mount_dir": str(root / "mount"),
                "custom_command": (
                    "rclone mount {remote_spec} {mount_dir} --bwlimit 1M:2M"
                ),
            }
            with (
                patch("talaryn.mount.ensure_runtime_dirs"),
                patch(
                    "talaryn.mount.cache_dir_for",
                    return_value=root / "cache",
                ),
                patch(
                    "talaryn.mount.log_file_for",
                    return_value=root / "example.log",
                ),
                patch(
                    "talaryn.mount.rc_socket_for",
                    return_value=root / "example.sock",
                ),
            ):
                command = build_mount_command("example", profile)

        self.assertEqual(command.count("--bwlimit"), 1)
        self.assertEqual(command[command.index("--bwlimit") + 1], "1M:2M")

    @patch(
        "talaryn.mount.load_settings",
        return_value={
            "upload_speed_limit": "",
            "download_speed_limit": "",
        },
    )
    def test_custom_command_cannot_expose_rc_over_tcp(
        self,
        _load_settings: object,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            socket_path = root / "example.sock"
            profile = {
                "remote_spec": "remote:",
                "mount_dir": str(root / "mount"),
                "custom_command": (
                    "rclone mount {remote_spec} {mount_dir} "
                    "--rc --rc-addr :5572 "
                    "--rc-addr=http://0.0.0.0:9999 --rc-no-auth"
                ),
            }
            with (
                patch("talaryn.mount.ensure_runtime_dirs"),
                patch(
                    "talaryn.mount.cache_dir_for",
                    return_value=root / "cache",
                ),
                patch(
                    "talaryn.mount.log_file_for",
                    return_value=root / "example.log",
                ),
                patch(
                    "talaryn.mount.rc_socket_for",
                    return_value=socket_path,
                ),
            ):
                command = build_mount_command("example", profile)

        self.assertNotIn(":5572", command)
        self.assertFalse(any("0.0.0.0" in part for part in command))
        self.assertEqual(command.count("--rc"), 1)
        self.assertEqual(command.count("--rc-addr"), 1)
        self.assertEqual(
            command[command.index("--rc-addr") + 1],
            f"unix://{socket_path}",
        )
        self.assertEqual(command.count("--rc-no-auth"), 1)

    def test_command_log_redacts_credentials(self) -> None:
        command = format_command_for_log(
            [
                "rclone",
                "mount",
                "remote:",
                "/mnt/cloud",
                "--s3-secret-access-key",
                "top secret",
                "--rc-pass=hunter2",
                "token=oauth-value",
                "https://user:password@example.test/path",
            ]
        )

        for secret in (
            "top secret",
            "hunter2",
            "oauth-value",
            "user:password",
        ):
            self.assertNotIn(secret, command)
        self.assertIn("<redacted>", command)
        self.assertIn("remote:", command)

    def test_command_log_redacts_shell_command_body(self) -> None:
        command = format_command_for_log(
            ["bash", "-lc", "rclone mount remote: /mnt --password cleartext"]
        )

        self.assertNotIn("cleartext", command)
        self.assertNotIn("rclone mount", command)
        self.assertIn("<redacted>", command)

    def test_clear_logs_removes_rotations_only_for_selected_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            selected = (
                root / "example-2026-08-01T12-00-00.000.log.gz"
            )
            unrelated = root / "example-2.log"
            selected.touch()
            unrelated.touch()
            (root / "example.log").touch()
            (root / "example.command.log.1").touch()

            with patch("talaryn.mount.CACHE_DIR", root):
                cleared = clear_logs_for("example")

            self.assertTrue(cleared)
            self.assertFalse(selected.exists())
            self.assertFalse((root / "example.log").exists())
            self.assertFalse((root / "example.command.log.1").exists())
            self.assertTrue(unrelated.exists())

    def test_show_existing_file_selects_it_in_nautilus(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "report.txt"
            path.touch()
            with (
                patch(
                    "talaryn.mount.command_exists",
                    return_value=True,
                ),
                patch("talaryn.mount.subprocess.Popen") as popen,
            ):
                show_file_in_nautilus(str(path))

        self.assertEqual(
            popen.call_args.args[0],
            ["nautilus", "--select", str(path)],
        )

    def test_show_missing_file_opens_its_parent_in_nautilus(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "deleted.txt"
            with (
                patch(
                    "talaryn.mount.command_exists",
                    return_value=True,
                ),
                patch("talaryn.mount.subprocess.Popen") as popen,
            ):
                show_file_in_nautilus(str(path))

        self.assertEqual(
            popen.call_args.args[0],
            ["nautilus", str(path.parent)],
        )


if __name__ == "__main__":
    unittest.main()
