from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ElementTree

from talaryn.icons import (
    DEFAULT_REMOTE_ICON_NAME,
    available_icon_names,
    icon_path,
    icon_storage_dir,
    remote_icon_path,
)
from talaryn.paths import (
    APP_ICON_FILE,
    APP_ICON_NAME,
    APP_INDICATOR_ICON_NAME,
)


class IconTests(unittest.TestCase):
    def test_custom_icons_are_separate_from_managed_assets(self) -> None:
        self.assertEqual(icon_storage_dir().name, "custom-icons")

    def test_missing_provider_icon_uses_generic_remote_storage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            icon_dir = root / "icons"
            icon_dir.mkdir()
            fallback = icon_dir / DEFAULT_REMOTE_ICON_NAME
            fallback.touch()

            with patch("talaryn.icons.data_dirs", return_value=[root]):
                self.assertEqual(
                    remote_icon_path("provider-without-artwork.svg"),
                    str(fallback),
                )
                self.assertEqual(icon_path(None), str(fallback))

    def test_google_drive_uses_generic_remote_storage_artwork(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            icon_dir = root / "icons"
            icon_dir.mkdir()
            fallback = icon_dir / DEFAULT_REMOTE_ICON_NAME
            fallback.touch()

            with patch("talaryn.icons.data_dirs", return_value=[root]):
                self.assertIsNone(icon_path("google-drive.svg"))
                self.assertEqual(
                    remote_icon_path("google-drive.svg"),
                    str(fallback),
                )

    def test_gnome_logo_text_icons_are_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            icon_dir = root / "icons"
            icon_dir.mkdir()
            (icon_dir / "onedrive.svg").touch()
            (icon_dir / "gnome-logo-text.svg").touch()
            (icon_dir / "gnome-logo-text-dark.svg").touch()

            with patch(
                "talaryn.icons.data_dirs",
                return_value=[root],
            ):
                self.assertEqual(
                    available_icon_names(),
                    ["onedrive.svg"],
                )

    def test_legacy_provider_icons_use_generic_artwork_even_if_old_files_remain(self) -> None:
        defaults = {
            "dropbox.svg": "remote-storage.svg",
            "Glyph_128.svg": "remote-storage.svg",
            "seafile.svg": "remote-storage.svg",
            "fastmail.svg": "webdav.svg",
            "FM-Icon-RGB.svg": "webdav.svg",
            "nextcloud.svg": "webdav.svg",
            "logo_nextcloud_blue.svg": "webdav.svg",
            "logo_nextcloud_white.svg": "webdav.svg",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            icon_dir = root / "icons"
            icon_dir.mkdir()
            for name in set(defaults.values()):
                (icon_dir / name).touch()
            for stale_dir in (icon_dir, icon_dir / "3rd_party", root / "custom-icons"):
                stale_dir.mkdir(exist_ok=True)
                for name in defaults:
                    (stale_dir / name).touch()
            with patch("talaryn.icons.data_dirs", return_value=[root]):
                for name, default in defaults.items():
                    with self.subTest(name=name):
                        self.assertEqual(icon_path(name), str(icon_dir / default))
                        self.assertEqual(remote_icon_path(name), str(icon_dir / default))
                self.assertEqual(set(available_icon_names()), set(defaults.values()))

    def test_obsolete_third_party_directory_is_not_searched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            obsolete = root / "icons" / "3rd_party"
            obsolete.mkdir(parents=True)
            (obsolete / "old-provider.svg").touch()
            with patch("talaryn.icons.data_dirs", return_value=[root]):
                self.assertIsNone(icon_path("old-provider.svg"))
                self.assertEqual(available_icon_names(), [])

    def test_talaryn_vector_brand_assets_are_available(self) -> None:
        self.assertEqual(APP_ICON_NAME, "talaryn")
        self.assertEqual(APP_ICON_FILE, "talaryn-app-icon.svg")
        self.assertEqual(APP_INDICATOR_ICON_NAME, "talaryn-app-icon")
        for name in (
            "talaryn.svg",
            "talaryn-app-icon.svg",
        ):
            with self.subTest(name=name):
                path = icon_path(name)
                self.assertIsNotNone(path)
                root = ElementTree.parse(str(path)).getroot()
                self.assertEqual(root.tag.rsplit("}", 1)[-1], "svg")
                embedded_images = [
                    element
                    for element in root.iter()
                    if element.tag.rsplit("}", 1)[-1] == "image"
                ]
                self.assertEqual(embedded_images, [])

    def test_primary_logo_uses_outlined_letters_and_reference_mark(self) -> None:
        path = icon_path("talaryn.svg")
        self.assertIsNotNone(path)
        root = ElementTree.parse(str(path)).getroot()
        elements = list(root.iter())
        self.assertEqual(
            [item for item in elements if item.tag.rsplit("}", 1)[-1] == "text"],
            [],
        )
        paths = [
            item for item in elements if item.tag.rsplit("}", 1)[-1] == "path"
        ]
        self.assertEqual(len(paths), 3)
        self.assertEqual(paths[-1].get("fill"), "url(#brand)")

    def test_redundant_wordmark_asset_is_not_kept(self) -> None:
        source_icons = Path(__file__).resolve().parents[1] / "data" / "icons"
        self.assertFalse((source_icons / "talaryn-wordmark.svg").exists())

    def test_horizontal_logo_is_reserved_for_the_readme(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        readme = (project_root / "README.md").read_text(encoding="utf-8")
        self.assertIn('src="./data/icons/talaryn.svg"', readme)
        self.assertIn('alt="Talaryn"', readme)

        with patch("talaryn.icons.data_dirs", return_value=[project_root]):
            self.assertNotIn("talaryn.svg", available_icon_names())


if __name__ == "__main__":
    unittest.main()
