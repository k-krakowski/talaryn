"""GTK3 tray checks run separately from the GTK4 application test process."""
import subprocess
import sys
import unittest


class TraySafetyTests(unittest.TestCase):
    def test_unknown_status_waits_and_force_is_explicit(self):
        script = """
from unittest.mock import Mock, patch
from talaryn.tray import TrayApp
from talaryn.tray import Gtk
tray = Mock()
tray._pending_unmount = {"drive"}
with patch("talaryn.tray.get_sync_activity", return_value={"profiles": {}}):
    TrayApp._finish_pending_unmounts(tray)
tray._perform_unmount.assert_not_called()
assert tray._pending_unmount == {"drive"}
with patch("talaryn.tray.probe_transfer_state", return_value="unknown"), \\
     patch("talaryn.tray.systemd_stop") as stop, \\
     patch("talaryn.tray.notify"):
    TrayApp._perform_unmount(tray, "drive")
stop.assert_not_called()
assert tray._pending_unmount == {"drive"}
TrayApp._on_unmount_warning_response(tray, Mock(), Gtk.ResponseType.OK, "drive")
tray._perform_unmount.assert_called_once_with("drive", force=True)
"""
        result = subprocess.run([sys.executable, '-B', '-c', script],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_comparison_setting_refreshes_menu_and_controls_launch(self):
        script = """
from unittest.mock import Mock, patch
from talaryn.tray import TrayApp
tray = Mock()
tray._ordered_profiles.return_value = []
tray.menu.get_children.return_value = []
with patch("talaryn.tray.list_profiles", return_value={}), \\
     patch("talaryn.tray.get_sync_activity", return_value={}):
    for enabled in (False, True, False):
        tray._append_item.reset_mock()
        with patch("talaryn.tray.load_settings", return_value={"context_file_comparison": enabled}), \\
             patch("talaryn.tray.subprocess.Popen") as launch:
            TrayApp.refresh_menu(tray)
            callbacks = [call.args[1] for call in tray._append_item.call_args_list]
            assert (tray._open_comparison in callbacks) == enabled
            assert TrayApp._current_menu_state(tray) == tray._menu_state
            TrayApp._open_comparison(tray)
            assert launch.called == enabled
"""
        result = subprocess.run([sys.executable, '-B', '-c', script],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
