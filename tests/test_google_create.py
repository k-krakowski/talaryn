from __future__ import annotations

from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from talaryn import google_create as creation
from talaryn.window_position import clamp_position
from talaryn.activity_store import ActivityStore


class GoogleCreationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.profile = {"kind": "gdrive", "mount_dir": str(self.folder),
                        "remote_spec": "cloud:mounted/root", "custom_command": "--drive-export-formats link.html"}
        self.store = ActivityStore(self.folder / "history.db")
        store_patch = patch.object(creation, "ActivityStore", return_value=self.store)
        store_patch.start()
        self.addCleanup(store_patch.stop)
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
        self.assertEqual(self.store.activity_count(), 0)

    def test_all_google_kinds_refresh_and_find_the_link_before_success(self):
        for kind, ext in (("doc", "docx"), ("sheet", "xlsx"), ("slides", "pptx")):
            link = self.folder / "Budget.link.html"
            link.touch()
            stages = []
            with patch.object(creation, "_run", side_effect=[
                subprocess.CompletedProcess([], 4, "", ""),
                subprocess.CompletedProcess([], 0, "", '{"msg":"Copied (new)"}'),
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
            event = self.store.recent(1, operation="create")[0]
            self.assertEqual(event["google_kind"], kind)
            self.assertEqual(event["path"], "Budget.link.html")
            self.assertEqual(event["profile_id"], "google")
            self.assertEqual(event["state"], "completed")

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
            subprocess.CompletedProcess([], 0, "", '{"msg":"Copied (new)"}'),
            subprocess.CompletedProcess([], 0, '{"ID":"google-id"}', ""),
        ]) as run, patch.object(creation, "rc_call", return_value=None):
            result = self.create()
        self.assertTrue(result.created)
        self.assertTrue(result.refresh_warning)
        self.assertTrue(result.detail)
        self.assertIsNone(result.local_path)
        event = self.store.recent(1)[0]
        self.assertEqual(event["path"], "Budget.link.html")
        self.assertEqual(event["state"], "completed")
        self.assertEqual(sum(call.args[0][1] == "copyto" for call in run.call_args_list), 1)

    def test_missing_id_does_not_turn_created_document_into_upload_failure(self):
        (self.folder / "Budget.link.html").touch()
        with patch.object(creation, "_run", side_effect=[
            subprocess.CompletedProcess([], 4, "", ""),
            subprocess.CompletedProcess([], 0, "", '{"msg":"Copied (new)"}'),
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
                return subprocess.CompletedProcess([], 0, "", '{"msg":"Copied (new)"}')
            if run_calls[0] == 0:
                run_calls[0] += 1
                return subprocess.CompletedProcess([], 4, "", "")
            visible.assert_called_once_with(link)
            return subprocess.CompletedProcess([], 0, '{"ID":"id"}', "")
        run_calls = [0]
        with patch.object(creation, "_run", side_effect=run), \
             patch.object(creation, "rc_call", return_value={"result": {"": "OK"}}):
            creation.create_google_file("google", self.profile, self.folder, "doc", "Budget", visible=visible)

    def test_creation_history_persists_and_enters_the_monitor_snapshot(self):
        from talaryn.sync_status import collect_sync_activity, group_activity_events
        folder = self.folder / "projects"
        folder.mkdir()
        link = folder / "Budget.link.html"
        link.touch()
        self.profile["label"] = "My Google Drive"
        with patch.object(creation, "_run", side_effect=[
            subprocess.CompletedProcess([], 4, "", ""),
            subprocess.CompletedProcess([], 0, "", '{"msg":"Copied (new)"}'),
            subprocess.CompletedProcess([], 0, '{"ID":"id"}', ""),
        ]), patch.object(creation, "rc_call", return_value={"result": {"projects": "OK"}}):
            result = creation.create_google_file("google", self.profile, folder, "sheet", "Budget")
            # Refresh is also used by the UI's retry button; it must not log another creation.
            creation.refresh_created_file("google", self.profile, folder, "Budget", "xlsx")
        self.assertTrue(result.created)
        reopened = ActivityStore(self.store.path)
        self.assertEqual(reopened.activity_count(), 1)
        activity = collect_sync_activity(profiles={}, store=reopened)
        event = activity["recent"][0]
        self.assertEqual(event["path"], "projects/Budget.link.html")
        self.assertEqual(event["name"], "Budget.link.html")
        self.assertEqual(event["profile_label"], "My Google Drive")
        self.assertEqual(event["local_path"], str(link))
        self.assertEqual(event["direction"], "remote")
        self.assertFalse(event["size_known"])
        self.assertEqual(activity["history_total"], 1)
        self.assertEqual(group_activity_events(activity["recent"])[0]["operation"], "create")
        self.assertEqual(len(reopened.recent(10, operation="create")), 1)
        self.assertEqual(reopened.recent(10, operation="copy"), [])

    def test_failed_or_skipped_upload_never_adds_a_success_event(self):
        for code, log in ((5, "upload failed"), (0, ""),
                          (0, '{"msg":"Skipped copy as --dry-run is set"}')):
            with patch.object(creation, "_run", side_effect=[
                subprocess.CompletedProcess([], 4, "", ""),
                subprocess.CompletedProcess([], code, "", log),
            ]) as run:
                result = self.create()
            self.assertFalse(result.created)
            self.assertEqual(self.store.activity_count(), 0)
            self.assertEqual(run.call_count, 2)

    def test_history_storage_error_keeps_document_created_and_warns_user(self):
        import sqlite3
        with patch.object(creation, "_run", side_effect=[
            subprocess.CompletedProcess([], 4, "", ""),
            subprocess.CompletedProcess([], 0, "", '{"msg":"Copied (new)"}'),
            subprocess.CompletedProcess([], 0, '{"ID":"id"}', ""),
        ]), patch.object(creation, "refresh_created_file", return_value=self.folder / "Budget.link.html"), \
             patch.object(self.store, "save_events", side_effect=sqlite3.OperationalError("disk full")):
            result = self.create()
        self.assertTrue(result.created)
        self.assertIn("disk full", result.detail)
        self.assertEqual(self.store.activity_count(), 0)

    @unittest.skipUnless(shutil.which("rclone"), "rclone is not installed")
    def test_real_rclone_new_upload_adds_exactly_one_creation_event(self):
        self.profile["remote_spec"] = ":local:" + str(self.folder)
        self.profile["custom_command"] = "--drive-export-formats=docx"
        with patch.object(creation, "rc_call", return_value={"result": {"": "OK"}}):
            result = self.create()
        destination = self.folder / "Budget.docx"
        self.assertTrue(result.created)
        self.assertEqual(destination.read_bytes(), (creation.TEMPLATE_DIR / "blank.docx").read_bytes())
        events = self.store.recent(10)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["path"], "Budget.docx")
        self.assertEqual(events[0]["operation"], "create")

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
                result = self.create()
            self.assertFalse(result.created)
            self.assertEqual(self.store.activity_count(), 0)
            self.assertEqual(destination.read_bytes(), b"existing document")

    def test_position_stays_inside_monitor_even_near_edges(self):
        self.assertEqual(clamp_position((1919, 1079), (460, 280), (0, 0, 1920, 1080)), (1460, 800))
        self.assertEqual(clamp_position((-1910, 200), (460, 280), (-1920, 0, 1920, 1080)), (-1898, 212))
