from __future__ import annotations

import io
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

from talaryn import main
from talaryn.app import MainWindow, RemoteManagerDialog
from talaryn.monitor import finish_pending_unmounts
from talaryn.transfer_safety import (
    probe_transfer_state, snapshot_allows_unmount, transfer_state,
)
from talaryn.sync_status import get_sync_activity, read_sync_snapshot


def idle_vfs():
    return {"diskCache": {"uploadsQueued": 0, "uploadsInProgress": 0,
                          "erroredFiles": 0}}


def snapshot(state="idle", age=0):
    return {"updated_at": time.time() - age,
            "profiles": {"drive": {"connected": True, "transfer_state": state}}}


class TransferSafetyTests(unittest.TestCase):
    def test_idle_requires_complete_successful_responses(self):
        self.assertEqual(transfer_state({"queue": []}, idle_vfs(), {}), "idle")
        for queue, vfs, core in (
            (None, idle_vfs(), {}), ({}, idle_vfs(), {}),
            ({"queue": []}, None, {}), ({"queue": []}, {}, {}),
            ({"queue": []}, {"diskCache": {}}, {}),
            ({"queue": []}, idle_vfs(), None),
            ({"queue": "bad"}, idle_vfs(), {}),
        ):
            with self.subTest(queue=queue, vfs=vfs, core=core):
                self.assertEqual(transfer_state(queue, vfs, core), "unknown")

    def test_unfiltered_queue_and_all_upload_counters_block_unmount(self):
        for name in ("file.txt", ".goutputstream-123", ".Trash-1000/files/file"):
            self.assertEqual(transfer_state({"queue": [{"name": name}]}, idle_vfs(), {}), "busy")
        for key in idle_vfs()["diskCache"]:
            vfs = idle_vfs()
            vfs["diskCache"][key] = 1
            self.assertEqual(transfer_state({"queue": []}, vfs, {}), "busy")
            for bad in (-1, "0", None, True):
                vfs["diskCache"][key] = bad
                self.assertEqual(transfer_state({"queue": []}, vfs, {}), "unknown")

    def test_cache_disabled_still_checks_active_transfers(self):
        vfs = {"opt": {"CacheMode": "off"}}
        self.assertEqual(transfer_state(None, vfs, {}), "idle")
        self.assertEqual(transfer_state(None, vfs, {"transferring": [{"name": "a"}]}), "busy")

    def test_probe_rechecks_rc_and_does_not_use_snapshot(self):
        with patch("talaryn.transfer_safety.rc_call", side_effect=[{"queue": []}, idle_vfs(), {}]) as rc:
            self.assertEqual(probe_transfer_state("drive"), "idle")
        self.assertEqual([call.args[1] for call in rc.call_args_list],
                         ["vfs/queue", "vfs/stats", "core/stats"])

    def test_only_fresh_connected_idle_snapshot_can_trigger_automatic_unmount(self):
        self.assertTrue(snapshot_allows_unmount(snapshot(), "drive"))
        for state in ("busy", "unknown", None):
            self.assertFalse(snapshot_allows_unmount(snapshot(state), "drive"))
        for age in (13, -10):
            self.assertFalse(snapshot_allows_unmount(snapshot(age=age), "drive"))
        for value in ("bad", None, float("nan"), float("inf")):
            data = snapshot(); data["updated_at"] = value
            self.assertFalse(snapshot_allows_unmount(data, "drive"))
        for data in ({}, {"updated_at": time.time(), "profiles": {}},
                     {"updated_at": time.time(), "profiles": {"drive": {"is_syncing": False}}}):
            self.assertFalse(snapshot_allows_unmount(data, "drive"))

    def test_missing_monitor_is_explicitly_unknown(self):
        with patch("talaryn.sync_status.read_sync_snapshot", return_value=None):
            data = get_sync_activity({"drive": {}})
        self.assertEqual(data["profiles"]["drive"]["transfer_state"], "unknown")
        self.assertFalse(snapshot_allows_unmount(data, "drive"))

    def test_corrupt_snapshot_timestamp_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            path.write_text('{"updated_at": "not a number"}')
            with patch("talaryn.sync_status.SYNC_SNAPSHOT_FILE", path):
                self.assertIsNone(read_sync_snapshot())


class UnmountCallerTests(unittest.TestCase):
    def test_monitor_keeps_waiting_when_status_unknown_stale_or_changes(self):
        for activity, live in ((snapshot("unknown"), "idle"),
                               (snapshot(age=30), "idle"),
                               (snapshot(), "busy"), (snapshot(), "unknown")):
            with self.subTest(activity=activity, live=live), \
                patch("talaryn.monitor.pending_unmount_profiles", return_value={"drive"}), \
                patch("talaryn.monitor.probe_transfer_state", return_value=live), \
                patch("talaryn.monitor.systemd_stop") as stop, \
                patch("talaryn.monitor.unmount_raw") as raw, \
                patch("talaryn.monitor.set_profile_pending_unmount") as pending:
                finish_pending_unmounts(activity, {"drive": {}})
                stop.assert_not_called(); raw.assert_not_called(); pending.assert_not_called()

    def test_monitor_stops_only_after_fresh_idle_and_live_idle(self):
        with patch("talaryn.monitor.pending_unmount_profiles", return_value={"drive"}), \
             patch("talaryn.monitor.probe_transfer_state", return_value="idle"), \
             patch("talaryn.monitor.systemd_stop", return_value=True) as stop, \
             patch("talaryn.monitor.clear_mount_folder_icon"), \
             patch("talaryn.monitor.set_profile_pending_unmount") as pending:
            finish_pending_unmounts(snapshot(), {"drive": {}})
        stop.assert_called_once_with("drive")
        pending.assert_called_once_with("drive", False)

    def test_gui_unknown_status_displays_warning_instead_of_unmount(self):
        window = Mock()
        with patch("talaryn.app.get_sync_activity", return_value=snapshot("unknown")), \
             patch("talaryn.app.Adw.AlertDialog.new") as dialog:
            MainWindow.request_unmount_profile(window, "drive")
        window.unmount_profile.assert_not_called()
        dialog.return_value.present.assert_called_once_with(window)

    def test_gui_rechecks_live_status_and_preserves_wait_request(self):
        window = Mock(); window._pending_unmount = {"drive"}
        with patch("talaryn.app.probe_transfer_state", return_value="busy"), \
             patch("talaryn.app.systemd_stop") as stop, \
             patch("talaryn.app.set_profile_pending_unmount") as pending, \
             patch("talaryn.app.show_message"):
            MainWindow.unmount_profile(window, "drive")
        stop.assert_not_called(); pending.assert_not_called()
        self.assertEqual(window._pending_unmount, {"drive"})

    def test_gui_force_choice_is_explicit(self):
        window = Mock()
        MainWindow._on_unmount_warning_response(window, Mock(), "unmount", "drive")
        window.unmount_profile.assert_called_once_with("drive", force=True)

    def test_removal_preflights_all_profiles_before_stopping_any(self):
        manager = Mock(); manager._profile_is_running.return_value = True
        with patch("talaryn.app.probe_transfer_state", side_effect=["idle", "unknown"]), \
             patch("talaryn.app.systemd_stop") as stop, \
             patch("talaryn.app.set_profile_desired_mounted") as desired:
            result = RemoteManagerDialog._stop_profiles_for_removal(manager, {"a": {}, "b": {}})
        self.assertFalse(result); stop.assert_not_called(); desired.assert_not_called()

    def test_removal_rechecks_before_stopping(self):
        manager = Mock(); manager._profile_is_running.return_value = True
        with patch("talaryn.app.probe_transfer_state", side_effect=["idle", "busy"]), \
             patch("talaryn.app.systemd_stop") as stop:
            self.assertFalse(RemoteManagerDialog._stop_profiles_for_removal(manager, {"a": {}}))
        stop.assert_not_called()

    def test_remote_configuration_is_preserved_when_removal_blocked(self):
        manager = Mock(); manager._stop_profiles_for_removal.return_value = False
        with patch("talaryn.app.profiles_for_remote", return_value={"drive": {}}), \
             patch("talaryn.app.delete_remote_config") as delete, \
             patch("talaryn.app.delete_profiles") as profiles:
            RemoteManagerDialog._on_delete_response(manager, Mock(), "delete", "remote")
        delete.assert_not_called(); profiles.assert_not_called()


class CliSafetyTests(unittest.TestCase):
    def setUp(self):
        targets = {
            "ensure_runtime_dirs": None, "installed_rclone_version": (1, 74, 4),
            "get_profile": {"mount_dir": "/tmp/test"}, "notify": None,
            "set_profile_pending_unmount": None, "set_profile_desired_mounted": None,
            "clear_mount_folder_icon": None, "systemd_stop": True,
            "unmount_raw": True, "delete_profile": None,
            "is_profile_mounted": False, "systemd_is_active": False,
        }
        self.mocks = {}
        for name, value in targets.items():
            patcher = patch("talaryn.main." + name, return_value=value)
            self.mocks[name] = patcher.start(); self.addCleanup(patcher.stop)

    def test_busy_and_unknown_unmount_do_not_change_state(self):
        for state in ("busy", "unknown"):
            with patch("talaryn.main.probe_transfer_state", return_value=state), \
                 redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main.main(["unmount", "drive"])
            self.assertEqual(error.exception.code, 2)
        self.mocks["systemd_stop"].assert_not_called()
        self.mocks["unmount_raw"].assert_not_called()
        self.mocks["set_profile_desired_mounted"].assert_not_called()

    def test_idle_and_explicit_force_can_unmount(self):
        for args, state in ((["unmount", "drive"], "idle"),
                            (["unmount", "drive", "--force"], "unknown")):
            with patch("talaryn.main.probe_transfer_state", return_value=state), \
                 self.assertRaises(SystemExit) as error:
                main.main(args)
            self.assertEqual(error.exception.code, 0)
        self.assertEqual(self.mocks["systemd_stop"].call_count, 2)

    def test_forget_refuses_mounted_or_active_profiles(self):
        for mounted, active in ((True, False), (False, True)):
            self.mocks["is_profile_mounted"].return_value = mounted
            self.mocks["systemd_is_active"].return_value = active
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main.main(["forget", "drive"])
            self.assertEqual(error.exception.code, 2)
        self.mocks["delete_profile"].assert_not_called()

    def test_forget_clears_automount_state_for_stopped_profile(self):
        main.main(["forget", "drive"])
        self.mocks["delete_profile"].assert_called_once_with("drive")
        self.mocks["set_profile_desired_mounted"].assert_called_once_with("drive", False)
