from __future__ import annotations

from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from talaryn import google_create as creation
from talaryn.window_position import clamp_position


class GoogleCreationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.profile = {"kind": "gdrive", "mount_dir": str(self.folder),
                        "remote_spec": "cloud:mounted/root", "custom_command": "--drive-export-formats link.html"}
        self.template_patch = patch.object(creation, "TEMPLATE_DIR", self.folder / "templates")
        self.template_patch.start()
        self.addCleanup(self.template_patch.stop)
        sleep = patch.object(creation.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def create(self, kind="doc", title="Budget", progress=lambda _: None):
        return creation.create_google_file("google", self.profile, self.folder, kind, title, progress)

    def test_invalid_names_and_paths_never_start_upload(self):
        with patch.object(creation, "_run") as run:
            for title in ("", "  ", ".", "..", "line\nname", "null\x00name"):
                self.assertFalse(self.create(title=title).created)
            outside = creation.create_google_file("google", self.profile, "/elsewhere", "doc", "New")
            self.assertFalse(outside.created)
        run.assert_not_called()
        self.assertEqual(creation.clean_title("  Budget.link.html "), "Budget")
        self.assertEqual(creation.clean_title("Budget/2026.xlsx"), "Budget-2026")

    def test_existing_document_and_network_error_never_upload(self):
        for code in (0, 5):
            with patch.object(creation, "_run", return_value=subprocess.CompletedProcess([], code, "", "network error")) as run:
                result = self.create()
            self.assertFalse(result.created)
            run.assert_called_once()

    def test_all_google_kinds_refresh_and_find_the_link_before_success(self):
        for kind, ext in (("doc", "docx"), ("sheet", "xlsx"), ("slides", "pptx")):
            link = self.folder / "Budget.link.html"
            link.touch()
            stages = []
            with patch.object(creation, "_run", side_effect=[
                subprocess.CompletedProcess([], 4, "", ""),
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 0, '{"ID":"google-id"}', ""),
            ]) as run, patch.object(creation, "rc_call", return_value={"result": {"": "OK"}}) as rc:
                result = self.create(kind=kind, progress=stages.append)
            self.assertTrue(result.created)
            self.assertEqual(result.local_path, link)
            self.assertFalse(result.refresh_warning)
            self.assertEqual(result.detail, "")
            self.assertIn("google-id", result.url)
            upload = run.call_args_list[1].args[0]
            self.assertIn("--ignore-existing", upload)
            self.assertIn(f"--drive-import-formats={ext}", upload)
            self.assertIn(f"cloud:mounted/root/Budget.{ext}", upload)
            rc.assert_called_once_with("google", "vfs/refresh", {"dir": ""}, timeout=5)
            self.assertEqual(len(stages), 4)

    def test_subfolder_refresh_is_relative_to_mount_not_remote_root(self):
        folder = self.folder / "projects" / "2026"
        folder.mkdir(parents=True)
        link = folder / "Budget.link.html"
        link.touch()
        with patch.object(creation, "rc_call", return_value={"result": {"projects/2026": "OK"}}) as rc:
            path = creation.refresh_created_file("google", self.profile, folder, "Budget", "docx")
        self.assertEqual(path, link)
        rc.assert_called_once_with("google", "vfs/refresh", {"dir": "projects/2026"}, timeout=5)

    def test_custom_export_format_is_found_without_reading_document_content(self):
        self.profile["custom_command"] = 'rclone mount "cloud:" /mount --drive-export-formats=pdf,docx'
        pdf = self.folder / "Budget.pdf"
        pdf.touch()
        with patch.object(creation, "rc_call", return_value={"result": {"": "OK"}}):
            self.assertEqual(creation.refresh_created_file("google", self.profile, self.folder, "Budget", "docx"), pdf)

    def test_refresh_http_success_with_directory_error_is_not_success(self):
        (self.folder / "Budget.link.html").touch()
        with patch.object(creation, "rc_call", return_value={"result": {"": "directory not found"}}) as rc:
            self.assertIsNone(creation.refresh_created_file("google", self.profile, self.folder, "Budget", "docx"))
        self.assertEqual(rc.call_count, 3)

    def test_successful_refresh_retries_until_file_is_visible(self):
        def refresh(*args, **kwargs):
            if rc.call_count == 2:
                (self.folder / "Budget.link.html").touch()
            return {"result": {"": "OK"}}
        with patch.object(creation, "rc_call", side_effect=refresh) as rc:
            result = creation.refresh_created_file("google", self.profile, self.folder, "Budget", "docx")
        self.assertEqual(result, self.folder / "Budget.link.html")
        self.assertEqual(rc.call_count, 2)

    def test_refresh_failure_reports_created_and_does_not_repeat_upload(self):
        with patch.object(creation, "_run", side_effect=[
            subprocess.CompletedProcess([], 4, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, '{"ID":"google-id"}', ""),
        ]) as run, patch.object(creation, "rc_call", return_value=None):
            result = self.create()
        self.assertTrue(result.created)
        self.assertTrue(result.refresh_warning)
        self.assertTrue(result.detail)
        self.assertIsNone(result.local_path)
        self.assertEqual(sum(call.args[0][1] == "copyto" for call in run.call_args_list), 1)

    def test_missing_id_does_not_turn_created_document_into_upload_failure(self):
        (self.folder / "Budget.link.html").touch()
        with patch.object(creation, "_run", side_effect=[
            subprocess.CompletedProcess([], 4, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            *[subprocess.CompletedProcess([], 0, '{}', "") for _ in range(3)],
        ]), patch.object(creation, "rc_call", return_value={"result": {"": "OK"}}):
            result = self.create()
        self.assertTrue(result.created)
        self.assertFalse(result.refresh_warning)
        self.assertIsNone(result.url)
        self.assertTrue(result.detail)

    def test_nautilus_is_notified_before_waiting_for_google_file_id(self):
        link = self.folder / "Budget.link.html"
        link.touch()
        visible = Mock()
        def run(command, _timeout):
            if command[1] == "copyto":
                return subprocess.CompletedProcess([], 0, "", "")
            if run_calls[0] == 0:
                run_calls[0] += 1
                return subprocess.CompletedProcess([], 4, "", "")
            visible.assert_called_once_with(link)
            return subprocess.CompletedProcess([], 0, '{"ID":"id"}', "")
        run_calls = [0]
        with patch.object(creation, "_run", side_effect=run), \
             patch.object(creation, "rc_call", return_value={"result": {"": "OK"}}):
            creation.create_google_file("google", self.profile, self.folder, "doc", "Budget", visible=visible)

    @unittest.skipUnless(shutil.which("rclone"), "rclone is not installed")
    def test_real_rclone_preserves_existing_file_and_a_file_created_during_preflight(self):
        # Exercise real copyto --ignore-existing locally without contacting Google.
        self.profile["remote_spec"] = ":local:" + str(self.folder)
        destination = self.folder / "Budget.docx"
        real_run = creation._run
        for during_preflight in (False, True):
            if destination.exists():
                destination.unlink()
            if not during_preflight:
                destination.write_bytes(b"existing document")
            def run(command, timeout):
                result = real_run(command, timeout)
                if during_preflight and command[1] == "lsjson":
                    destination.write_bytes(b"existing document")
                return result
            with patch.object(creation, "_run", side_effect=run), patch.object(creation, "rc_call", return_value=None):
                self.create()
            self.assertEqual(destination.read_bytes(), b"existing document")

    def test_position_stays_inside_monitor_even_near_edges(self):
        self.assertEqual(clamp_position((1919, 1079), (460, 280), (0, 0, 1920, 1080)), (1460, 800))
        self.assertEqual(clamp_position((-1910, 200), (460, 280), (-1920, 0, 1920, 1080)), (-1898, 212))
