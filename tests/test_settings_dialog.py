from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from talaryn.app import COLOR_SCHEME_IDS, LANGUAGE_IDS, MainWindow, SettingsDialog
from talaryn.config import DEFAULT_SETTINGS


class SettingsDialogTests(unittest.TestCase):
    def test_save_applies_settings_and_closes_without_restart(self) -> None:
        window = Mock()
        window.settings = dict(DEFAULT_SETTINGS)
        dialog = SimpleNamespace(
            settings=dict(window.settings),
            on_saved=lambda settings: MainWindow._after_settings_saved(window, settings),
            close=Mock(),
            _show_error=Mock(),
            _settings_combo_id=SettingsDialog._settings_combo_id,
        )
        for field in (
            "automount", "auto_remount", "disable_tray", "minimize_tray",
            "open_after_mount", "show_hidden_profiles", "notify_complete",
            "notify_errors", "confirm_close", "inhibit_shutdown",
            "context_open_terminal", "context_copy_remote_path",
            "context_google_new_docs", "context_file_comparison",
        ):
            setattr(dialog, f"{field}_switch", Mock(get_active=Mock(return_value=False)))
        dialog.automount_switch.get_active.return_value = True
        dialog.show_hidden_profiles_switch.get_active.return_value = True
        dialog.upload_limit_entry = Mock(get_text=Mock(return_value=""))
        dialog.download_limit_entry = Mock(get_text=Mock(return_value=""))
        dialog.terminal_entry = Mock(get_text=Mock(return_value=""))
        dialog.language_combo = Mock(get_selected=Mock(return_value=LANGUAGE_IDS.index("system")))
        dialog.color_scheme_combo = Mock(get_selected=Mock(return_value=COLOR_SCHEME_IDS.index("dark")))
        patterns_buffer = Mock(get_text=Mock(return_value="\n".join(DEFAULT_SETTINGS["activity_hidden_patterns"])))
        dialog.activity_patterns_view = Mock(get_buffer=Mock(return_value=patterns_buffer))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("talaryn.config.SETTINGS_FILE", root / "settings.json"),
                patch("talaryn.config.AUTOSTART_FILE", root / "autostart" / "talaryn.desktop"),
                patch("talaryn.config.ensure_runtime_dirs"),
                patch("talaryn.config.shutil.which", return_value="/usr/bin/talaryn"),
                patch("talaryn.app._apply_color_scheme") as apply_color_scheme,
                patch("talaryn.app.show_message") as show_message,
            ):
                SettingsDialog._on_save_clicked(dialog, Mock())

            saved = json.loads((root / "settings.json").read_text())

        self.assertTrue(saved["automount_previous"])
        self.assertTrue(saved["show_hidden_profiles"])
        self.assertEqual(saved["color_scheme"], "dark")
        self.assertEqual(window.settings, saved)
        apply_color_scheme.assert_called_once_with("dark")
        window.refresh.assert_called_once_with()
        window._ensure_tray_running.assert_called_once_with()
        dialog.close.assert_called_once_with()
        dialog._show_error.assert_not_called()
        show_message.assert_not_called()
