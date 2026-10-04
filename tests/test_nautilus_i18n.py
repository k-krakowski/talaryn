from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
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


class GoogleCreationLauncherTests(unittest.TestCase):
    def test_dialog_receives_profile_kind_and_position_without_a_name_prompt(self):
        context = {"pointer": [210, 350], "parent": "x11:abc"}
        command = GOOGLE._creation_command("google", Path("/cloud/google/folder"), "sheet", context)
        self.assertIn("-I", command)
        index = command.index("google-create")
        self.assertEqual(command[index + 1:index + 4], ["google", "/cloud/google/folder", "sheet"])
        self.assertEqual(json.loads(command[index + 4]), context)

    def test_creation_refreshes_the_originating_nautilus_window(self):
        from unittest.mock import Mock
        parent = Mock()
        parent.activate_action.return_value = True
        GOOGLE._refresh_nautilus(parent, "/cloud/google/New.link.html")
        parent.activate_action.assert_called_once_with("win.reload", None)

    def test_end_of_child_output_does_not_schedule_an_infinite_read_loop(self):
        from unittest.mock import Mock
        provider = GOOGLE.GoogleNewDocsProvider()
        for line in (None, b""):
            stream = Mock()
            stream.read_line_finish.return_value = (line, 0)
            provider._read_result(stream, Mock(), Mock())
            stream.read_line_async.assert_not_called()
