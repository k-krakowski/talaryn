from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from talaryn.rclone import (
    _parse_redacted_config,
    RcloneCommandResult,
    RcloneCommandRunner,
    continue_remote_config,
    create_remote_config,
    default_custom_command,
    delete_remote_config,
    detect_kind,
    icon_for_kind,
    list_remote_dirs,
    normalize_remote_name,
    redacted_remote_config,
    remote_name_exists,
    sftp_no_hashcheck_command,
    ssh_development_command,
    test_remote_connection,
    update_remote_config,
)


class RcloneConfigTests(unittest.TestCase):
    def test_sftp_editor_command_avoids_aggressive_cache_scanning(self) -> None:
        command = ssh_development_command()

        self.assertIn("--vfs-write-back 1s", command)
        self.assertIn("--vfs-links", command)
        self.assertNotIn("--links ", command)
        self.assertNotIn("--vfs-cache-poll-interval", command)

    def test_sftp_no_hashcheck_is_an_explicit_mount_preset(self) -> None:
        command = sftp_no_hashcheck_command()

        self.assertEqual(command.count("--sftp-disable-hashcheck"), 1)
        self.assertTrue(command.startswith(default_custom_command("sftp")))

    def test_config_transport_integration_uses_temporary_rclone_config(self) -> None:
        if shutil.which("rclone") is None:
            self.skipTest("rclone is not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            probe_path = root / "socket-probe"
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.bind(str(probe_path))
            except PermissionError:
                self.skipTest("Unix sockets are disabled by the test sandbox")
            finally:
                probe.close()
                probe_path.unlink(missing_ok=True)
            config_path = root / "rclone.conf"
            with (
                patch(
                    "talaryn.rclone.runtime_dir",
                    return_value=root,
                ),
                patch.dict(
                    os.environ,
                    {"RCLONE_CONFIG": str(config_path)},
                    clear=False,
                ),
            ):
                step = create_remote_config(
                    "transport-probe",
                    "local",
                    {},
                    RcloneCommandRunner(),
                )

            self.assertTrue(step.complete, step.error)
            config_text = config_path.read_text(encoding="utf-8")

        self.assertIn("[transport-probe]", config_text)
        self.assertIn("type = local", config_text)

    def test_config_transport_keeps_secrets_out_of_process_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket_path = root / f"config-{os.getpid()}-fixed.sock"
            socket_path.touch()
            process = Mock()
            process.poll.return_value = None
            process.stderr = Mock()
            response = Mock(status=200)
            response.read.return_value = (
                b'{"State":"","Option":null,"Error":"","Result":""}'
            )
            connection = Mock()
            connection.getresponse.return_value = response

            with (
                patch(
                    "talaryn.rclone.runtime_dir",
                    return_value=root,
                ),
                patch(
                    "talaryn.rclone.secrets.token_hex",
                    return_value="fixed",
                ),
                patch(
                    "talaryn.rclone.subprocess.Popen",
                    return_value=process,
                ) as popen,
                patch(
                    "talaryn.rclone.UnixHTTPConnection",
                    return_value=connection,
                ),
                patch.dict(
                    os.environ,
                    {
                        "RCLONE_RC_ADDR": ":5572",
                        "RCLONE_CONFIG": "/tmp/config",
                        "RCLONE_DUMP": "headers,bodies",
                        "RCLONE_LOG_FILE": "/tmp/unsafe-rclone.log",
                        "RCLONE_VERBOSE": "2",
                    },
                    clear=True,
                ),
            ):
                result = RcloneCommandRunner().run_config(
                    "config/update",
                    {
                        "name": "cloud",
                        "parameters": {"pass": "top secret"},
                    },
                )

        self.assertTrue(result.ok)
        command = popen.call_args.args[0]
        self.assertNotIn("top secret", " ".join(command))
        environment = popen.call_args.kwargs["env"]
        self.assertNotIn("RCLONE_RC_ADDR", environment)
        self.assertNotIn("RCLONE_DUMP", environment)
        self.assertNotIn("RCLONE_LOG_FILE", environment)
        self.assertNotIn("RCLONE_VERBOSE", environment)
        self.assertEqual(environment["RCLONE_CONFIG"], "/tmp/config")
        self.assertIs(
            popen.call_args.kwargs["stderr"],
            subprocess.DEVNULL,
        )
        request_body = connection.request.call_args.kwargs["body"]
        self.assertIn(b"top secret", request_body)

    def test_config_transport_reports_socket_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket_path = root / f"config-{os.getpid()}-fixed.sock"
            socket_path.touch()
            process = Mock()
            process.poll.return_value = None
            connection = Mock()
            connection.request.side_effect = TimeoutError("request timed out")

            with (
                patch(
                    "talaryn.rclone.runtime_dir",
                    return_value=root,
                ),
                patch(
                    "talaryn.rclone.secrets.token_hex",
                    return_value="fixed",
                ),
                patch(
                    "talaryn.rclone.subprocess.Popen",
                    return_value=process,
                ),
                patch(
                    "talaryn.rclone.UnixHTTPConnection",
                    return_value=connection,
                ),
            ):
                result = RcloneCommandRunner().run_config(
                    "config/update",
                    {"name": "cloud", "parameters": {}},
                )

        self.assertEqual(result.returncode, 124)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.ok)

    def test_runner_cancellation_is_consumed_by_one_operation(self) -> None:
        first_process = Mock(returncode=-15)
        first_process.poll.return_value = None
        first_process.communicate.return_value = ("", "")
        second_process = Mock(returncode=0)
        second_process.poll.return_value = None
        second_process.communicate.return_value = ("ok", "")
        runner = RcloneCommandRunner()
        runner.cancel()

        with patch(
            "talaryn.rclone.subprocess.Popen",
            side_effect=(first_process, second_process),
        ):
            first = runner.run(["rclone", "version"])
            second = runner.run(["rclone", "version"])

        self.assertTrue(first.cancelled)
        first_process.terminate.assert_called_once_with()
        self.assertFalse(second.cancelled)
        second_process.terminate.assert_not_called()

    def test_remote_name_normalization(self) -> None:
        self.assertEqual(normalize_remote_name(" Team Drive: "), "Team Drive")
        for value in (
            "",
            ":",
            "--config=/tmp/other.conf",
            "bad/name",
            "bad\\name",
            "bad:name:",
            "bad\nname",
        ):
            with self.subTest(value=value):
                self.assertIsNone(normalize_remote_name(value))

    @patch("talaryn.rclone.command_exists", return_value=True)
    def test_invalid_remote_name_stops_config_commands(
        self,
        _command_exists: Mock,
    ) -> None:
        runner = Mock()

        created = create_remote_config(
            "--config=/tmp/other.conf",
            "drive",
            {"scope": "drive"},
            runner,
        )
        continued = continue_remote_config(
            "bad/name",
            "state",
            "answer",
            {},
            runner,
        )
        updated = update_remote_config(
            "bad:name",
            {"host": "example.test"},
            runner,
        )
        checked = test_remote_connection("bad\nname", runner)

        self.assertFalse(created.command_ok)
        self.assertFalse(continued.command_ok)
        self.assertFalse(updated.command_ok)
        self.assertFalse(checked.ok)
        runner.run.assert_not_called()
        runner.run_config.assert_not_called()

    @patch("talaryn.rclone.run_capture")
    @patch("talaryn.rclone.command_exists", return_value=True)
    def test_invalid_remote_name_stops_read_delete_and_list_commands(
        self,
        _command_exists: Mock,
        run_capture: Mock,
    ) -> None:
        deleted, delete_error = delete_remote_config("--config=/tmp/other")
        config, read_error = redacted_remote_config("bad/name")
        directories, list_error = list_remote_dirs("bad:name", "folder")

        self.assertFalse(deleted)
        self.assertTrue(delete_error)
        self.assertEqual(config, {})
        self.assertTrue(read_error)
        self.assertEqual(directories, [])
        self.assertTrue(list_error)
        run_capture.assert_not_called()

    def test_remote_kinds_use_generic_category_icons(self) -> None:
        self.assertEqual(icon_for_kind("sftp"), "sftp.svg")
        self.assertEqual(icon_for_kind("ftp"), "ftp.svg")
        self.assertEqual(icon_for_kind("smb"), "smb.svg")
        self.assertEqual(icon_for_kind("webdav"), "webdav.svg")
        self.assertEqual(icon_for_kind("b2"), "object-storage.svg")
        self.assertEqual(icon_for_kind("protondrive"), "remote-storage.svg")
        self.assertEqual(icon_for_kind("mega"), "remote-storage.svg")
        self.assertEqual(icon_for_kind("iclouddrive"), "remote-storage.svg")
        self.assertEqual(icon_for_kind("seafile"), "remote-storage.svg")
        self.assertEqual(icon_for_kind("dropbox"), "remote-storage.svg")
        self.assertEqual(icon_for_kind("nextcloud"), "webdav.svg")
        self.assertEqual(icon_for_kind("fastmail"), "webdav.svg")
        self.assertEqual(icon_for_kind("gdrive"), "remote-storage.svg")
        self.assertEqual(icon_for_kind("drive"), "remote-storage.svg")
        self.assertEqual(
            icon_for_kind("google cloud storage"),
            "object-storage.svg",
        )
        self.assertEqual(
            icon_for_kind("azureblob"),
            "object-storage.svg",
        )
        self.assertEqual(icon_for_kind("s3"), "object-storage.svg")
        self.assertEqual(icon_for_kind("unknown"), "remote-storage.svg")

    def test_nextcloud_webdav_vendor_uses_nextcloud_kind(self) -> None:
        self.assertEqual(
            detect_kind("personal", "webdav", "nextcloud"),
            "nextcloud",
        )

    def test_fastmail_webdav_vendor_uses_fastmail_kind(self) -> None:
        self.assertEqual(
            detect_kind("mail-files", "webdav", "fastmail"),
            "fastmail",
        )

    def test_google_cloud_storage_is_not_treated_as_google_drive(self) -> None:
        self.assertEqual(
            detect_kind("google-cloud-storage", "google cloud storage"),
            "google cloud storage",
        )

    @patch("talaryn.rclone.command_exists", return_value=True)
    def test_create_remote_parses_configuration_question(
        self,
        _command_exists: Mock,
    ) -> None:
        runner = Mock()
        runner.run_config.return_value = RcloneCommandResult(
            0,
            json.dumps(
                {
                    "State": "*oauth-islocal,teamdrive,,",
                    "Option": {
                        "Name": "config_is_local",
                        "Type": "bool",
                        "Default": True,
                    },
                    "Error": "",
                    "Result": "",
                }
            ),
            "",
        )

        step = create_remote_config(
            "my-drive",
            "drive",
            {"scope": "drive", "client_id": ""},
            runner,
        )

        self.assertTrue(step.command_ok)
        self.assertFalse(step.complete)
        self.assertEqual(step.state, "*oauth-islocal,teamdrive,,")
        self.assertEqual(step.option["Name"], "config_is_local")
        method, payload = runner.run_config.call_args.args
        self.assertEqual(method, "config/create")
        self.assertEqual(payload["name"], "my-drive")
        self.assertEqual(payload["type"], "drive")
        self.assertEqual(payload["parameters"], {"scope": "drive"})
        self.assertTrue(payload["opt"]["nonInteractive"])
        self.assertTrue(payload["opt"]["obscure"])

    @patch("talaryn.rclone.command_exists", return_value=True)
    def test_create_remote_recognizes_completed_configuration(
        self,
        _command_exists: Mock,
    ) -> None:
        runner = Mock()
        runner.run_config.return_value = RcloneCommandResult(
            0,
            '{"State":"","Option":null,"Error":"","Result":""}',
            "",
        )

        step = create_remote_config(
            "server",
            "sftp",
            {"host": "example.test", "key_use_agent": "true"},
            runner,
        )

        self.assertTrue(step.complete)

    @patch("talaryn.rclone.command_exists", return_value=True)
    def test_continue_remote_passes_state_and_answer(
        self,
        _command_exists: Mock,
    ) -> None:
        runner = Mock()
        runner.run_config.return_value = RcloneCommandResult(
            0,
            '{"State":"","Option":null,"Error":"","Result":""}',
            "",
        )

        step = continue_remote_config(
            "cloud",
            "*oauth-islocal,teamdrive,,",
            "true",
            {"scope": "drive"},
            runner,
        )

        self.assertTrue(step.complete)
        method, payload = runner.run_config.call_args.args
        self.assertEqual(method, "config/update")
        self.assertEqual(payload["name"], "cloud")
        self.assertEqual(payload["opt"]["result"], "true")
        self.assertEqual(
            payload["opt"]["state"],
            "*oauth-islocal,teamdrive,,",
        )
        self.assertTrue(payload["opt"]["continue"])

    @patch("talaryn.rclone.command_exists", return_value=True)
    def test_update_remote_uses_non_interactive_protocol(
        self,
        _command_exists: Mock,
    ) -> None:
        runner = Mock()
        runner.run_config.return_value = RcloneCommandResult(
            0,
            '{"State":"","Option":null,"Error":"","Result":""}',
            "",
        )

        step = update_remote_config(
            "server:",
            {"host": "example.test", "pass": "new password"},
            runner,
        )

        self.assertTrue(step.complete)
        method, payload = runner.run_config.call_args.args
        self.assertEqual(method, "config/update")
        self.assertEqual(payload["name"], "server")
        self.assertEqual(payload["parameters"]["host"], "example.test")
        self.assertEqual(payload["parameters"]["pass"], "new password")
        self.assertTrue(payload["opt"]["nonInteractive"])
        self.assertTrue(payload["opt"]["obscure"])

    @patch("talaryn.rclone.command_exists", return_value=True)
    def test_update_remote_can_clear_obsolete_options(
        self,
        _command_exists: Mock,
    ) -> None:
        runner = Mock()
        runner.run_config.return_value = RcloneCommandResult(
            0,
            '{"State":"","Option":null,"Error":"","Result":""}',
            "",
        )

        update_remote_config(
            "server",
            {"key_use_agent": "true"},
            runner,
            clear_keys={"pass", "key_file"},
        )

        _method, payload = runner.run_config.call_args.args
        self.assertEqual(payload["parameters"]["pass"], "")
        self.assertEqual(payload["parameters"]["key_file"], "")

    @patch("talaryn.rclone.command_exists", return_value=True)
    def test_update_remote_can_clear_an_explicit_empty_option(
        self,
        _command_exists: Mock,
    ) -> None:
        runner = Mock()
        runner.run_config.return_value = RcloneCommandResult(
            0,
            '{"State":"","Option":null,"Error":"","Result":""}',
            "",
        )

        update_remote_config(
            "storage",
            {"endpoint": "", "provider": "AWS"},
            runner,
            clear_keys={"endpoint"},
        )

        _method, payload = runner.run_config.call_args.args
        self.assertEqual(payload["parameters"]["endpoint"], "")

    def test_redacted_config_parser_keeps_only_redacted_values(self) -> None:
        configs = _parse_redacted_config(
            "[cloud]\n"
            "type = drive\n"
            "client_id = XXX\n"
            "token = XXX\n"
            "scope = drive\n"
        )

        self.assertEqual(
            configs["cloud"],
            {
                "type": "drive",
                "client_id": "XXX",
                "token": "XXX",
                "scope": "drive",
            },
        )

    @patch("talaryn.rclone.run_capture")
    @patch("talaryn.rclone.command_exists", return_value=True)
    def test_read_remote_uses_redacted_config(
        self,
        _command_exists: Mock,
        run_capture: Mock,
    ) -> None:
        run_capture.return_value = (
            0,
            "[cloud]\ntype = drive\ntoken = XXX\n",
            "",
        )

        config, error = redacted_remote_config("CLOUD:")

        self.assertEqual(error, "")
        self.assertEqual(config["token"], "XXX")
        run_capture.assert_called_once_with(
            ["rclone", "config", "redacted", "CLOUD"]
        )

    @patch("talaryn.rclone.command_exists", return_value=True)
    def test_connection_check_uses_remote_root(
        self,
        _command_exists: Mock,
    ) -> None:
        runner = Mock()
        runner.run.return_value = RcloneCommandResult(0, "", "")

        result = test_remote_connection("cloud:", runner, timeout=12)

        self.assertTrue(result.ok)
        command = runner.run.call_args.args[0]
        self.assertEqual(command[:3], ["rclone", "lsd", "cloud:"])
        self.assertEqual(runner.run.call_args.kwargs["timeout"], 12)

    @patch(
        "talaryn.rclone.list_remotes",
        return_value=[{"name": "Cloud"}],
    )
    def test_remote_name_conflict_is_case_insensitive(
        self,
        _list_remotes: Mock,
    ) -> None:
        self.assertTrue(remote_name_exists("cloud:"))
        self.assertFalse(remote_name_exists("other"))

if __name__ == "__main__":
    unittest.main()
