from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from talaryn import config, main


class ComparisonSettingsTests(unittest.TestCase):
    def test_default_is_disabled_and_saved_preferences_are_preserved(self):
        self.assertFalse(config.DEFAULT_SETTINGS['context_file_comparison'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            with patch('talaryn.config.SETTINGS_FILE', path), \
                 patch('talaryn.config.ensure_runtime_dirs'), \
                 patch('talaryn.config.set_tray_autostart_enabled'):
                self.assertFalse(config.load_settings()['context_file_comparison'])
                for enabled in (True, False):
                    config.save_settings({'context_file_comparison': enabled})
                    self.assertEqual(config.load_settings()['context_file_comparison'], enabled)

    def test_cli_launch_requires_comparison_to_be_enabled(self):
        for settings in ({}, {'context_file_comparison': False}, {'context_file_comparison': True}):
            enabled = settings.get('context_file_comparison', False)
            with self.subTest(settings=settings), \
                 patch('talaryn.main.ensure_runtime_dirs'), \
                 patch('talaryn.main.installed_rclone_version', return_value=(1, 74, 4)), \
                 patch('talaryn.main.load_settings', return_value=settings), \
                 patch('talaryn.main.apply_toolkit_language'), \
                 patch('talaryn.main.notify'), \
                 patch('talaryn.comparison_dialog.run_file_comparison', return_value=0) as run, \
                 redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main.main(['compare', '/tmp/example.txt'])
            self.assertEqual(error.exception.code, 0 if enabled else 2)
            if enabled:
                run.assert_called_once_with(['/tmp/example.txt'])
            else:
                run.assert_not_called()
