from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest
from unittest.mock import patch

from test_nautilus_emblems import _load_nautilus_extension

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "test_extension_i18n", ROOT / "data/nautilus/talaryn_i18n.py"
)
I18N = importlib.util.module_from_spec(spec)
spec.loader.exec_module(I18N)
GOOGLE = _load_nautilus_extension("test_google_new_docs", "google_new_docs.py")


class NautilusTranslationTests(unittest.TestCase):
    def test_saved_language_overrides_locale_and_formats_values(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "talaryn"
            config.mkdir()
            (config / "settings.json").write_text('{"language": "ru"}')
            with patch.dict(os.environ, {
                "XDG_CONFIG_HOME": directory,
                "TALARYN_DATA_DIR": str(ROOT),
                "LANGUAGE": "pl",
            }, clear=True):
                self.assertEqual(I18N.tr("nautilus_compare"), "Сравнить")
                self.assertEqual(I18N.tr("file_not_found", path="/tmp/test"),
                                 "Файл не найден: /tmp/test")

    def test_invalid_settings_and_unsupported_locale_fall_back_to_english(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "talaryn"
            config.mkdir()
            (config / "settings.json").write_text('[]')
            with patch.dict(os.environ, {
                "XDG_CONFIG_HOME": directory,
                "TALARYN_DATA_DIR": str(ROOT),
                "LANG": "de_DE.UTF-8",
            }, clear=True):
                self.assertEqual(I18N.tr("nautilus_compare"), "Compare")

    def test_missing_translation_uses_english(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "i18n").mkdir()
            (root / "i18n/en.json").write_text('{"message": "Hello {name}"}')
            (root / "i18n/ru.json").write_text('{}')
            with patch.object(I18N, "_roots", return_value=(root,)), \
                 patch.object(I18N, "_language", return_value="ru"):
                self.assertEqual(I18N.tr("message", name="test"), "Hello test")


class GoogleCreationTests(unittest.TestCase):
    def test_cancelled_name_dialog_returns_no_name(self):
        provider = GOOGLE.GoogleNewDocsProvider()
        result = subprocess.CompletedProcess([], 1, stdout="")
        with patch.object(GOOGLE.subprocess, "run", return_value=result):
            self.assertIsNone(provider._ask_name("Document"))

    def test_cancellation_does_not_launch_creation_worker(self):
        provider = GOOGLE.GoogleNewDocsProvider()
        with tempfile.TemporaryDirectory() as directory:
            templates = Path(directory)
            (templates / "blank.docx").touch()
            with patch.object(GOOGLE, "TEMPLATE_DIR", templates), \
                 patch.object(provider, "_ask_name", return_value=None), \
                 patch.object(GOOGLE.subprocess, "Popen") as launch:
                provider._create_google_file(None, templates, {}, templates, "doc")
            launch.assert_not_called()

    @unittest.skipUnless(shutil.which("rclone"), "rclone is not installed")
    def test_new_document_does_not_overwrite_an_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "blank.docx"
            destination = root / "existing.docx"
            template.write_bytes(b"new")
            destination.write_bytes(b"existing document contents")
            payload = {
                "title": "existing", "dest": str(destination),
                "template": str(template), "remote_folder": str(root),
                "kind": "doc", "ext": "docx",
            }
            with patch.object(GOOGLE, "_start_progress", return_value=None), \
                 patch.object(GOOGLE, "_stop_progress"), \
                 patch.object(GOOGLE, "_notify"), \
                 patch.object(GOOGLE, "OPEN_AFTER_CREATE", False):
                self.assertNotEqual(GOOGLE._run_worker(payload), 0)
            self.assertEqual(destination.read_bytes(), b"existing document contents")

    def test_failed_existence_check_never_uploads(self):
        result = subprocess.CompletedProcess([], 5, stdout="", stderr="network error")
        payload = {
            "title": "new", "dest": "cloud:new.docx", "template": "/tmp/blank.docx",
            "remote_folder": "cloud:", "kind": "doc", "ext": "docx",
        }
        with patch.object(GOOGLE.subprocess, "run", return_value=result) as run, \
             patch.object(GOOGLE, "_notify"):
            self.assertEqual(GOOGLE._run_worker(payload), 5)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][1], "lsjson")

    @unittest.skipUnless(shutil.which("rclone"), "rclone is not installed")
    def test_file_created_after_preflight_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "blank.docx"
            destination = root / "new.docx"
            template.write_bytes(b"template contents")
            payload = {
                "title": "new", "dest": str(destination), "template": str(template),
                "remote_folder": str(root), "kind": "doc", "ext": "docx",
            }
            real_run = subprocess.run

            def run(command, **kwargs):
                result = real_run(command, **kwargs)
                if command[1] == "lsjson":
                    destination.write_bytes(b"concurrently created document")
                return result

            with patch.object(GOOGLE.subprocess, "run", side_effect=run), \
                 patch.object(GOOGLE, "_start_progress", return_value=None), \
                 patch.object(GOOGLE, "_stop_progress"), \
                 patch.object(GOOGLE, "_notify"), \
                 patch.object(GOOGLE, "OPEN_AFTER_CREATE", False):
                GOOGLE._run_worker(payload)
            self.assertEqual(destination.read_bytes(), b"concurrently created document")
