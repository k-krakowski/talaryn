from __future__ import annotations

import json
import os
from pathlib import Path
from string import Formatter
import tempfile
import unittest
from unittest.mock import patch

from talaryn.i18n import (
    SUPPORTED_LANGUAGES,
    apply_toolkit_language,
    configured_language,
    detect_language,
    load_translation,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def placeholders(text: str) -> set[str]:
    return {
        field_name
        for _, field_name, _, _ in Formatter().parse(text)
        if field_name is not None
    }


class LanguageTests(unittest.TestCase):
    def test_explicit_language_overrides_system_locale(self) -> None:
        with patch.dict(os.environ, {"LANG": "en_US.UTF-8"}, clear=True):
            self.assertEqual(detect_language("pl"), "pl")

    def test_system_language_uses_locale(self) -> None:
        with patch.dict(os.environ, {"LANG": "pl_PL.UTF-8"}, clear=True):
            self.assertEqual(detect_language("system"), "pl")

    def test_system_language_supports_new_locales(self) -> None:
        with patch.dict(os.environ, {"LANG": "ru_RU.UTF-8"}, clear=True):
            self.assertEqual(detect_language("system"), "ru")

    def test_removed_system_language_falls_back_to_english(self) -> None:
        with patch.dict(os.environ, {"LANG": "de_DE.UTF-8"}, clear=True):
            self.assertEqual(detect_language("system"), "en")

    def test_configured_language_reads_saved_setting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings_file = Path(directory) / "settings.json"
            settings_file.write_text(
                json.dumps({"language": "en"}),
                encoding="utf-8",
            )
            with patch(
                "talaryn.i18n.SETTINGS_FILE",
                settings_file,
            ):
                self.assertEqual(configured_language(), "en")

    def test_all_translation_catalogs_match_english(self) -> None:
        catalog_dir = PROJECT_ROOT / "i18n"
        english = json.loads(
            (catalog_dir / "en.json").read_text(encoding="utf-8")
        )

        for language in SUPPORTED_LANGUAGES:
            with self.subTest(language=language):
                catalog = json.loads(
                    (catalog_dir / f"{language}.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(set(catalog), set(english))
                for key, source in english.items():
                    self.assertEqual(
                        placeholders(catalog[key]),
                        placeholders(source),
                        key,
                    )

    def test_all_supported_languages_can_be_loaded(self) -> None:
        for language in SUPPORTED_LANGUAGES:
            with self.subTest(language=language):
                translation = load_translation(language)
                self.assertTrue(translation["settings"])
                self.assertTrue(translation["language_system"])

    def test_talaryn_name_is_consistent_across_languages(self) -> None:
        for language in SUPPORTED_LANGUAGES:
            with self.subTest(language=language):
                translation = load_translation(language)
                self.assertEqual(translation["app_title"], "Talaryn")

    def test_project_metadata_uses_the_brand_slogan_and_description(self) -> None:
        desktop = (
            PROJECT_ROOT
            / "data/applications/io.github.k_krakowski.Talaryn.desktop.in"
        ).read_text(encoding="utf-8")
        comparison_desktop = (
            PROJECT_ROOT
            / "data/applications/io.github.k_krakowski.Talaryn.Comparison.desktop.in"
        ).read_text(encoding="utf-8")
        metainfo = (
            PROJECT_ROOT
            / "data/metainfo/io.github.k_krakowski.Talaryn.metainfo.xml.in"
        ).read_text(encoding="utf-8")
        pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(
            encoding="utf-8"
        )

        self.assertIn("Name=Talaryn\n", desktop)
        self.assertIn("Comment=Give your cloud wings\n", desktop)
        self.assertIn("Exec=@EXEC@ compare\n", comparison_desktop)
        self.assertIn("Icon=talaryn\n", comparison_desktop)
        self.assertIn("NoDisplay=true\n", comparison_desktop)
        self.assertIn("<name>Talaryn</name>", metainfo)
        self.assertIn("<summary>Give your cloud wings</summary>", metainfo)
        description = (
            "Talaryn is a graphical Linux interface for mounting cloud and "
            "remote storage as local drives using rclone."
        )
        self.assertIn(description, pyproject)
        self.assertIn(
            "Talaryn is a graphical Linux interface for mounting cloud and remote\n"
            "      storage as local drives using rclone.",
            metainfo,
        )

    def test_selected_language_is_applied_to_gettext_toolkit(self) -> None:
        with patch.dict(os.environ, {"LANGUAGE": "pl:en"}):
            apply_toolkit_language("ru")
            self.assertEqual(os.environ["LANGUAGE"], "ru")

    def test_system_language_keeps_system_gettext_configuration(self) -> None:
        with patch.dict(os.environ, {"LANGUAGE": "pl:en"}):
            apply_toolkit_language("system")
            self.assertEqual(os.environ["LANGUAGE"], "pl:en")


if __name__ == "__main__":
    unittest.main()
