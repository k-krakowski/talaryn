from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from talaryn.google_create import CreationResult
from talaryn.google_create_dialog import GoogleCreateWindow


class GoogleCreateDialogTests(unittest.TestCase):
    def dialog(self):
        dialog = SimpleNamespace(running=False, result=None, title_text="Budget",
            profile_id="google", profile={}, folder="/mount/folder", kind="doc",
            entry=Mock(get_text=Mock(return_value="Budget")), status=Mock(),
            cancel=Mock(), create=Mock(), _busy=Mock(), _ready=Mock(),
            _report_visible=Mock(), close=Mock(), _retry_refresh=Mock(), _work=Mock(),
            _set_status=Mock(), _refresh_finished=Mock())
        return dialog

    def test_duplicate_create_click_does_not_start_another_worker(self):
        dialog = self.dialog()
        dialog.running = True
        with patch("talaryn.google_create_dialog.threading.Thread") as thread:
            GoogleCreateWindow._start(dialog, None)
        thread.assert_not_called()

    def test_empty_name_stays_in_the_naming_window_without_upload(self):
        dialog = self.dialog()
        dialog.entry.get_text.return_value = " "
        with patch("talaryn.google_create_dialog.threading.Thread") as thread:
            GoogleCreateWindow._start(dialog, None)
        thread.assert_not_called()
        dialog.status.set_text.assert_called_once()

    def test_created_document_can_only_start_a_refresh_worker(self):
        dialog = self.dialog()
        dialog.result = CreationResult(True, refresh_warning=True)
        with patch("talaryn.google_create_dialog.threading.Thread") as thread:
            GoogleCreateWindow._start(dialog, None)
        self.assertEqual(thread.call_args.kwargs["target"], dialog._retry_refresh)
        thread.return_value.start.assert_called_once()
        dialog.entry.get_text.assert_not_called()

    def test_upload_failure_keeps_window_open_and_name_editable(self):
        dialog = self.dialog()
        GoogleCreateWindow._finished(dialog, CreationResult(False, "network error"))
        dialog.entry.set_sensitive.assert_called_once_with(True)
        dialog.create.set_sensitive.assert_called_once_with(True)
        dialog.close.assert_not_called()
        self.assertIn("network error", dialog.status.set_text.call_args.args[0])

    def test_success_reports_visible_file_then_opens_browser_and_closes_same_window(self):
        dialog = self.dialog()
        result = CreationResult(True, local_path=Path("/mount/folder/Budget.link.html"),
                                url="https://docs.google.com/document/d/id/edit")
        with patch("talaryn.google_create_dialog.subprocess.Popen") as launch:
            GoogleCreateWindow._finished(dialog, result)
        dialog._report_visible.assert_called_once_with(result.local_path)
        self.assertEqual(launch.call_args.args[0], ["xdg-open", result.url])
        dialog.close.assert_called_once()

    def test_browser_failure_preserves_refresh_retry_after_successful_upload(self):
        dialog = self.dialog()
        result = CreationResult(True, "refresh failed", url="https://docs.google.com/", refresh_warning=True)
        with patch("talaryn.google_create_dialog.subprocess.Popen", side_effect=FileNotFoundError):
            GoogleCreateWindow._finished(dialog, result)
        dialog.entry.set_sensitive.assert_called_once_with(False)
        dialog.create.set_sensitive.assert_called_once_with(True)
        dialog.create.set_visible.assert_called_once_with(True)
        dialog.close.assert_not_called()

    def test_completed_refresh_never_runs_creation_again(self):
        dialog = self.dialog()
        with patch("talaryn.google_create_dialog.refresh_created_file", return_value=Path("/mount/folder/Budget.link.html")) as refresh, \
             patch("talaryn.google_create_dialog.create_google_file") as create, \
             patch("talaryn.google_create_dialog.GLib.idle_add"):
            GoogleCreateWindow._retry_refresh(dialog)
        create.assert_not_called()
        refresh.assert_called_once_with("google", {}, Path("/mount/folder"), "Budget", "docx")
