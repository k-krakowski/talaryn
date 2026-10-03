from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from talaryn.config import (
    DEFAULT_SETTINGS,
    delete_profiles,
    load_profiles_data,
    profiles_for_remote,
    save_profile,
    save_profiles_data,
    save_settings,
    update_sponsor_reminder_state,
)


class SettingsDefaultsTests(unittest.TestCase):
    def test_saving_automount_settings_creates_tray_autostart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_file = root / "settings.json"
            autostart_file = root / "autostart" / "talaryn.desktop"
            with (
                patch("talaryn.config.SETTINGS_FILE", settings_file),
                patch("talaryn.config.AUTOSTART_FILE", autostart_file),
                patch("talaryn.config.ensure_runtime_dirs"),
                patch("talaryn.config.shutil.which", return_value="/usr/bin/talaryn") as which,
            ):
                save_settings({"automount_previous": True, "color_scheme": "dark"})

            which.assert_called_once_with("talaryn")
            self.assertTrue(json.loads(settings_file.read_text())["automount_previous"])
            self.assertIn("Exec=/usr/bin/talaryn tray\n", autostart_file.read_text())

    def test_sponsor_reminder_state_preserves_mount_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            state_file.write_text(
                json.dumps(
                    {
                        "desired_mounted": ["drive"],
                        "pending_unmount": ["archive"],
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch("talaryn.config.STATE_FILE", state_file),
                patch("talaryn.config.ensure_runtime_dirs"),
            ):
                update_sponsor_reminder_state(
                    first_used_at=100.0,
                    remind_after=200.0,
                    disabled=True,
                )

            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(state["desired_mounted"], ["drive"])
            self.assertEqual(state["pending_unmount"], ["archive"])
            self.assertEqual(state["sponsor_first_used_at"], 100.0)
            self.assertEqual(state["sponsor_remind_after"], 200.0)
            self.assertTrue(state["sponsor_reminder_disabled"])

    def test_hidden_profiles_are_not_shown_by_default(self) -> None:
        self.assertFalse(DEFAULT_SETTINGS["show_hidden_profiles"])

    def test_transfer_shutdown_safeguards_are_opt_in(self) -> None:
        self.assertFalse(DEFAULT_SETTINGS["confirm_close_during_sync"])
        self.assertFalse(DEFAULT_SETTINGS["inhibit_shutdown_during_sync"])

    def test_profiles_are_matched_to_remote_case_insensitively(self) -> None:
        profiles = {
            "personal": {"remote_name": "OneDrive"},
            "work": {"remote_name": "onedrive:"},
            "google": {"remote_name": "gdrive"},
        }

        self.assertEqual(
            set(profiles_for_remote("ONEDRIVE", profiles)),
            {"personal", "work"},
        )

    def test_deleting_profiles_also_clears_mount_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profiles_file = root / "profiles.json"
            state_file = root / "state.json"
            profiles_file.write_text(
                json.dumps(
                    {
                        "profiles": {
                            "remove-a": {"remote_name": "onedrive"},
                            "remove-b": {"remote_name": "onedrive"},
                            "keep": {"remote_name": "gdrive"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            state_file.write_text(
                json.dumps(
                    {
                        "desired_mounted": ["remove-a", "keep"],
                        "pending_unmount": ["remove-b", "keep"],
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch(
                    "talaryn.config.PROFILES_FILE",
                    profiles_file,
                ),
                patch(
                    "talaryn.config.STATE_FILE",
                    state_file,
                ),
                patch("talaryn.config.ensure_runtime_dirs"),
            ):
                delete_profiles(["remove-a", "remove-b"])

            saved_profiles = json.loads(
                profiles_file.read_text(encoding="utf-8")
            )
            saved_state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(
                set(saved_profiles["profiles"]),
                {"keep"},
            )
            self.assertEqual(saved_state["desired_mounted"], ["keep"])
            self.assertEqual(saved_state["pending_unmount"], ["keep"])

    def test_profile_file_is_written_atomically_and_privately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profiles_file = Path(directory) / "profiles.json"
            with (
                patch(
                    "talaryn.config.PROFILES_FILE",
                    profiles_file,
                ),
                patch("talaryn.config.ensure_runtime_dirs"),
            ):
                save_profiles_data({"profiles": {"test": {}}})

            self.assertEqual(
                profiles_file.stat().st_mode & 0o777,
                0o600,
            )
            self.assertEqual(
                json.loads(profiles_file.read_text(encoding="utf-8")),
                {"profiles": {"test": {}}},
            )
            self.assertFalse(list(profiles_file.parent.glob("*.tmp")))

    def test_unsafe_profile_ids_are_not_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profiles_file = Path(directory) / "profiles.json"
            profiles_file.write_text(
                json.dumps(
                    {
                        "profiles": {
                            "safe-profile": {},
                            "../outside": {},
                        }
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch(
                    "talaryn.config.PROFILES_FILE",
                    profiles_file,
                ),
                patch("talaryn.config.ensure_runtime_dirs"),
            ):
                profiles = load_profiles_data()["profiles"]

            self.assertEqual(set(profiles), {"safe-profile"})

    def test_invalid_json_is_preserved_before_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profiles_file = Path(directory) / "profiles.json"
            profiles_file.write_text("not json", encoding="utf-8")
            with (
                patch(
                    "talaryn.config.PROFILES_FILE",
                    profiles_file,
                ),
                patch("talaryn.config.ensure_runtime_dirs"),
            ):
                save_profiles_data({"profiles": {}})

            backups = list(
                profiles_file.parent.glob("profiles.json.corrupt-*")
            )
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "not json")

    def test_parallel_profile_updates_do_not_lose_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profiles_file = Path(directory) / "profiles.json"
            with (
                patch(
                    "talaryn.config.PROFILES_FILE",
                    profiles_file,
                ),
                patch("talaryn.config.ensure_runtime_dirs"),
            ):
                workers = [
                    threading.Thread(
                        target=save_profile,
                        args=(f"profile-{index}", {"index": index}),
                    )
                    for index in range(20)
                ]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join()

                profiles = load_profiles_data()["profiles"]

            self.assertEqual(len(profiles), 20)
            self.assertEqual(profiles["profile-17"]["index"], 17)


if __name__ == "__main__":
    unittest.main()
