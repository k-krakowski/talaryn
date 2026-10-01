from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from talaryn.paths import (
    _ensure_private_directory,
    migrate_legacy_user_data,
    prepare_legacy_migration,
)


class PrivateDirectoryTests(unittest.TestCase):
    def test_private_directory_permissions_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private"
            path.mkdir(mode=0o775)

            self.assertEqual(_ensure_private_directory(path), path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_symbolic_link_is_not_accepted_as_private_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            link = root / "private"
            os.symlink(target, link)

            with self.assertRaises(RuntimeError):
                _ensure_private_directory(link)

    def test_legacy_directories_and_autostart_are_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_config = root / "config" / "rclone-mount-gui"
            config = root / "config" / "talaryn"
            legacy_cache = root / "cache" / "rclone-mount-gui"
            cache = root / "cache" / "talaryn"
            legacy_data = root / "data" / "rclone-mount-gui"
            data = root / "data" / "talaryn"
            legacy_autostart = root / "autostart" / (
                "io.github.rclonemountgui.RcloneMountGui.tray.desktop"
            )
            autostart = root / "autostart" / (
                "io.github.k_krakowski.Talaryn.tray.desktop"
            )

            legacy_config.mkdir(parents=True, mode=0o700)
            legacy_cache.mkdir(parents=True, mode=0o700)
            (legacy_data / "custom-icons").mkdir(parents=True, mode=0o700)
            legacy_autostart.parent.mkdir(parents=True)
            (legacy_config / "profiles.json").write_text(
                '{"profiles": {"cloud": {}}}',
                encoding="utf-8",
            )
            (legacy_cache / "activity.db").write_bytes(b"history")
            (legacy_cache / "sync-status.json").write_text(
                "stale",
                encoding="utf-8",
            )
            (legacy_cache / "nautilus-status.json").write_text(
                "stale",
                encoding="utf-8",
            )
            (legacy_data / "custom-icons" / "cloud.svg").write_text(
                "<svg/>",
                encoding="utf-8",
            )
            legacy_autostart.write_text(
                "\n".join(
                    (
                        "Name=rclone-mount-gui",
                        "Exec=rclone-mount-gui tray",
                        "Icon=rclone-mount-gui-icon",
                        "StartupWMClass=io.github.rclonemountgui.RcloneMountGui",
                    )
                ),
                encoding="utf-8",
            )

            migrate_legacy_user_data(
                legacy_config,
                config,
                legacy_cache,
                cache,
                legacy_data,
                data,
                legacy_autostart,
                autostart,
            )

            self.assertFalse(legacy_config.exists())
            self.assertFalse(legacy_cache.exists())
            self.assertEqual(
                (config / "profiles.json").read_text(encoding="utf-8"),
                '{"profiles": {"cloud": {}}}',
            )
            self.assertEqual((cache / "activity.db").read_bytes(), b"history")
            self.assertFalse((cache / "sync-status.json").exists())
            self.assertFalse((cache / "nautilus-status.json").exists())
            self.assertTrue((data / "custom-icons" / "cloud.svg").is_file())
            self.assertFalse(legacy_autostart.exists())
            migrated_autostart = autostart.read_text(encoding="utf-8")
            self.assertIn("Name=Talaryn", migrated_autostart)
            self.assertIn("Exec=talaryn tray", migrated_autostart)
            self.assertIn("Icon=talaryn", migrated_autostart)
            self.assertIn(
                "StartupWMClass=io.github.k_krakowski.Talaryn",
                migrated_autostart,
            )

    def test_existing_talaryn_directory_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_config = root / "old"
            config = root / "new"
            legacy_config.mkdir(mode=0o700)
            config.mkdir(mode=0o700)
            (legacy_config / "profiles.json").write_text(
                "legacy",
                encoding="utf-8",
            )
            (config / "profiles.json").write_text("current", encoding="utf-8")

            migrate_legacy_user_data(
                legacy_config_dir=legacy_config,
                config_dir=config,
                legacy_cache_dir=root / "missing-cache",
                cache_dir=root / "cache",
                legacy_data_dir=root / "missing-data",
                data_dir=root / "data",
                legacy_autostart_file=root / "missing.desktop",
                autostart_file=root / "new.desktop",
            )

            self.assertEqual(
                (config / "profiles.json").read_text(encoding="utf-8"),
                "current",
            )
            self.assertTrue(legacy_config.exists())

    def test_migration_is_blocked_while_legacy_mount_is_active(self) -> None:
        with (
            patch.dict(os.environ, {"TALARYN_DISABLE_LEGACY_MIGRATION": "0"}),
            patch("talaryn.paths._legacy_migration_needed", return_value=True),
            patch("talaryn.paths._stop_legacy_monitor") as stop_monitor,
            patch("talaryn.paths._legacy_mount_units_active", return_value=True),
            patch("talaryn.paths._legacy_lock_is_held", return_value=False),
        ):
            with self.assertRaisesRegex(RuntimeError, "Unmount all profiles"):
                prepare_legacy_migration()

        stop_monitor.assert_called_once_with()

    def test_migration_is_blocked_while_legacy_tray_is_active(self) -> None:
        with (
            patch.dict(os.environ, {"TALARYN_DISABLE_LEGACY_MIGRATION": "0"}),
            patch("talaryn.paths._legacy_migration_needed", return_value=True),
            patch("talaryn.paths._stop_legacy_monitor"),
            patch("talaryn.paths._legacy_mount_units_active", return_value=False),
            patch("talaryn.paths._legacy_lock_is_held", return_value=True),
        ):
            with self.assertRaisesRegex(RuntimeError, "panel icon"):
                prepare_legacy_migration()


if __name__ == "__main__":
    unittest.main()
