from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from talaryn.google_export import (
    EXPORT_FORMATS,
    _choose_destination,
    _export_command,
    _link_probe_command,
    google_export_remote_path,
    google_link_kind,
    google_link_remote_path,
    normalized_export_destination,
    run_google_export,
)


class GoogleExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = {
            "kind": "gdrive",
            "mount_dir": "/home/user/cloud/google",
            "remote_spec": "drive:mounted/root",
        }
        self.local_path = Path(
            "/home/user/cloud/google/folder/Budget.link.html"
        )

    def test_google_link_types_have_appropriate_export_formats(self) -> None:
        self.assertEqual(
            google_link_kind("https://docs.google.com/document/d/id/edit"),
            "document",
        )
        self.assertEqual(
            google_link_kind(
                "https://docs.google.com/spreadsheets/d/id/edit"
            ),
            "spreadsheet",
        )
        self.assertEqual(
            google_link_kind(
                "https://docs.google.com/presentation/d/id/edit"
            ),
            "presentation",
        )
        self.assertIn("docx", EXPORT_FORMATS["document"])
        self.assertIn("xlsx", EXPORT_FORMATS["spreadsheet"])
        self.assertIn("pptx", EXPORT_FORMATS["presentation"])
        self.assertIn("pdf", EXPORT_FORMATS["document"])
        self.assertIn("pdf", EXPORT_FORMATS["spreadsheet"])
        self.assertIn("pdf", EXPORT_FORMATS["presentation"])

    def test_remote_export_path_uses_the_selected_export_extension(self) -> None:
        self.assertEqual(
            google_link_remote_path(self.profile, self.local_path),
            "drive:mounted/root/folder/Budget.link.html",
        )
        self.assertEqual(
            google_export_remote_path(
                self.profile,
                self.local_path,
                "xlsx",
            ),
            "drive:mounted/root/folder/Budget.xlsx",
        )

    def test_paths_outside_the_profile_mount_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            google_link_remote_path(
                self.profile,
                "/tmp/Budget.link.html",
            )

    def test_destination_gets_exactly_one_selected_extension(self) -> None:
        self.assertEqual(
            normalized_export_destination("/tmp/Budget", "xlsx"),
            Path("/tmp/Budget.xlsx"),
        )
        self.assertEqual(
            normalized_export_destination("/tmp/Budget.pdf", "xlsx"),
            Path("/tmp/Budget.xlsx"),
        )
        self.assertEqual(
            normalized_export_destination("/tmp/Budget.xlsx", "xlsx"),
            Path("/tmp/Budget.xlsx"),
        )

    def test_normalized_existing_destination_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "Budget.pdf"
            target.write_text("original")
            for typed in (str(target.with_suffix("")), str(target.with_suffix(".xlsx")), str(target)):
                for accepted in (True, False):
                    with self.subTest(typed=typed, accepted=accepted):
                        chooser = subprocess.CompletedProcess([], 0, typed + "\n", "")
                        answer = subprocess.CompletedProcess([], 0 if accepted else 1, "", "")
                        with patch("talaryn.google_export._run", side_effect=[chooser, answer]) as run:
                            result = _choose_destination("Budget.link.html", "pdf")
                        self.assertEqual(result, target if accepted else None)
                        command = run.call_args_list[1].args[0]
                        self.assertIn("--question", command)
                        self.assertIn("--default-cancel", command)
                        self.assertTrue(any(str(target) in arg for arg in command))
                        self.assertEqual(target.read_text(), "original")

    def test_new_destination_does_not_require_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            typed = str(Path(directory) / "new")
            with patch("talaryn.google_export._run", return_value=subprocess.CompletedProcess([], 0, typed, "")) as run:
                self.assertEqual(_choose_destination("Budget.link.html", "pdf"), Path(typed + ".pdf"))
            run.assert_called_once()

    def test_dangling_symlink_also_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "Budget.pdf"
            target.symlink_to(Path(directory) / "missing")
            with patch("talaryn.google_export._run", side_effect=[
                subprocess.CompletedProcess([], 0, str(target), ""),
                subprocess.TimeoutExpired("zenity", 600),
            ]):
                self.assertIsNone(_choose_destination("Budget.link.html", "pdf"))

    def test_export_reads_link_then_exports_the_real_document(self) -> None:
        probe = subprocess.CompletedProcess(
            [],
            0,
            "https://docs.google.com/spreadsheets/d/id/edit",
            "",
        )
        exported = subprocess.CompletedProcess([], 0, "", "")
        destination = Path("/tmp/Budget.xlsx")
        with (
            patch(
                "talaryn.google_export._run",
                side_effect=(probe, exported),
            ) as run,
            patch(
                "talaryn.google_export._choose_format",
                return_value="xlsx",
            ),
            patch(
                "talaryn.google_export._choose_destination",
                return_value=destination,
            ),
            patch("talaryn.google_export._start_progress", return_value=None),
            patch("talaryn.google_export._stop_progress"),
            patch("talaryn.google_export.notify") as notification,
        ):
            result = run_google_export(self.profile, self.local_path)

        self.assertEqual(result, 0)
        self.assertEqual(
            run.call_args_list[0].args[0],
            _link_probe_command(
                "drive:mounted/root/folder/Budget.link.html"
            ),
        )
        self.assertEqual(
            run.call_args_list[1].args[0],
            _export_command(
                "drive:mounted/root/folder/Budget.xlsx",
                destination,
                "xlsx",
            ),
        )
        notification.assert_called_once()


if __name__ == "__main__":
    unittest.main()
