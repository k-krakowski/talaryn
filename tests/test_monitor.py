from __future__ import annotations

import unittest
from unittest.mock import ANY, Mock, patch

from talaryn.monitor import (
    ShutdownInhibitor,
    TransferNotificationBatcher,
    _format_transfer_duration,
    _notify_deleted_google_links,
    _stop_legacy_monitor_service,
    ensure_monitor_running,
)


class TransferNotificationBatcherTests(unittest.TestCase):
    @staticmethod
    def _event(
        number: int,
        *,
        folder: str = "folder",
        operation: str = "copy",
        size: int = 1024 * 1024,
    ) -> dict:
        return {
            "event_id": f"event-{number}",
            "profile_id": "drive",
            "path": f"{folder}/file-{number}.bin",
            "first_seen": 100.0,
            "group_timestamp": 100.0,
            "timestamp": 101.0 + number,
            "state": "completed",
            "operation": operation,
            "size": size,
        }

    def test_duration_uses_an_appropriate_compact_unit(self) -> None:
        self.assertEqual(_format_transfer_duration(0.2), "<1 s")
        self.assertEqual(_format_transfer_duration(42), "42 s")
        self.assertEqual(_format_transfer_duration(125), "2 min 5 s")
        self.assertEqual(_format_transfer_duration(3720), "1 h 2 min")
        self.assertEqual(_format_transfer_duration(90000), "1 d 1 h")

    def test_hundreds_of_files_create_one_group_notification(self) -> None:
        batcher = TransferNotificationBatcher()
        events = [self._event(number) for number in range(100)]
        # A duplicate record for the same path may be observed through both
        # the log and core/transferred; it must not inflate the summary.
        events.append(dict(events[-1], event_id="duplicate"))
        live = {
            **self._event(200),
            "path": "folder/pending.bin",
            "state": "queued",
        }
        settings = {"notify_transfer_complete": True}

        def translate(key: str, **values: object) -> str:
            if key == "notification_transfer_group_summary":
                return (
                    f"{values['count']}|{values['size']}|"
                    f"{values['duration']}"
                )
            return key

        with (
            patch("talaryn.monitor.notify") as notification,
            patch("talaryn.monitor.tr", side_effect=translate),
        ):
            batcher.update(
                {
                    "active": [],
                    "queued": [live],
                    "new_events": events,
                },
                settings,
                now=200.0,
            )
            notification.assert_not_called()

            batcher.update(
                {"active": [], "queued": [], "new_events": []},
                settings,
                now=206.0,
            )

        notification.assert_called_once_with(
            "notification_transfer_complete",
            "100|100.0 MiB|1 min 40 s",
        )

    def test_group_waits_for_files_completed_in_later_monitor_cycles(self) -> None:
        batcher = TransferNotificationBatcher()
        settings = {"notify_transfer_complete": True}

        def translate(key: str, **values: object) -> str:
            if key == "notification_transfer_group_summary":
                return str(values["count"])
            return key

        with (
            patch("talaryn.monitor.notify") as notification,
            patch("talaryn.monitor.tr", side_effect=translate),
        ):
            batcher.update(
                {
                    "active": [],
                    "queued": [],
                    "new_events": [self._event(1)],
                },
                settings,
                now=110.0,
            )
            batcher.update(
                {
                    "active": [],
                    "queued": [],
                    "new_events": [self._event(2, operation="modify")],
                },
                settings,
                now=113.0,
            )
            batcher.update(
                {"active": [], "queued": [], "new_events": []},
                settings,
                now=117.0,
            )
            notification.assert_not_called()
            batcher.update(
                {"active": [], "queued": [], "new_events": []},
                settings,
                now=119.0,
            )

            # A late event with the same activity group key must not create a
            # second desktop notification for that group.
            batcher.update(
                {
                    "active": [],
                    "queued": [],
                    "new_events": [self._event(3)],
                },
                settings,
                now=120.0,
            )
            batcher.update(
                {"active": [], "queued": [], "new_events": []},
                settings,
                now=126.0,
            )

        notification.assert_called_once_with(
            "notification_transfer_complete",
            "2",
        )

    def test_single_file_uses_singular_notification(self) -> None:
        batcher = TransferNotificationBatcher()
        settings = {"notify_transfer_complete": True}

        def translate(key: str, **values: object) -> str:
            if key == "notification_transfer_single_summary":
                return f"one|{values['size']}|{values['duration']}"
            return key

        with (
            patch("talaryn.monitor.notify") as notification,
            patch("talaryn.monitor.tr", side_effect=translate),
        ):
            batcher.update(
                {
                    "active": [],
                    "queued": [],
                    "new_events": [self._event(1)],
                },
                settings,
                now=110.0,
            )
            batcher.update(
                {"active": [], "queued": [], "new_events": []},
                settings,
                now=116.0,
            )

        notification.assert_called_once_with(
            "notification_transfer_complete",
            "one|1.0 MiB|2 s",
        )


class GoogleLinkDeletionNotificationTests(unittest.TestCase):
    def test_deleted_google_link_shows_recovery_warning(self) -> None:
        event = {
            "event_id": "delete-1",
            "profile_id": "google",
            "path": "folder/Budget.link.html",
            "name": "Budget.link.html",
            "state": "completed",
            "operation": "delete",
        }
        with (
            patch("talaryn.monitor.notify") as notification,
            patch(
                "talaryn.monitor.tr",
                side_effect=lambda key, **values: (
                    f"removed:{values['name']}"
                    if key == "google_link_deleted_single"
                    else key
                ),
            ),
        ):
            _notify_deleted_google_links(
                [event],
                {"google": {"kind": "gdrive"}},
            )

        notification.assert_called_once_with(
            "google_link_deleted_title",
            "removed:Budget.link.html",
        )

    def test_non_google_or_non_link_deletions_do_not_warn(self) -> None:
        events = [
            {
                "profile_id": "google",
                "path": "folder/file.pdf",
                "state": "completed",
                "operation": "delete",
            },
            {
                "profile_id": "onedrive",
                "path": "folder/Document.link.html",
                "state": "completed",
                "operation": "delete",
            },
        ]
        with patch("talaryn.monitor.notify") as notification:
            _notify_deleted_google_links(
                events,
                {
                    "google": {"kind": "gdrive"},
                    "onedrive": {"kind": "onedrive"},
                },
            )

        notification.assert_not_called()


class ShutdownInhibitorTests(unittest.TestCase):
    def test_legacy_monitor_is_stopped_during_migration(self) -> None:
        with patch("talaryn.monitor.subprocess.run") as run:
            _stop_legacy_monitor_service()

        run.assert_called_once_with(
            [
                "systemctl",
                "--user",
                "disable",
                "--now",
                "rclone-mount-gui-monitor.service",
            ],
            stdout=ANY,
            stderr=ANY,
            check=False,
            timeout=5,
        )

    def test_inhibitor_tracks_enabled_sync_state(self) -> None:
        inhibitor = ShutdownInhibitor()

        with (
            patch(
                "talaryn.monitor._open_shutdown_inhibitor",
                return_value=37,
            ) as open_inhibitor,
            patch("talaryn.monitor.os.close") as close_descriptor,
        ):
            inhibitor.update(enabled=False, syncing=True)
            inhibitor.update(enabled=True, syncing=False)
            self.assertFalse(inhibitor.active)
            open_inhibitor.assert_not_called()

            inhibitor.update(enabled=True, syncing=True)
            self.assertTrue(inhibitor.active)
            open_inhibitor.assert_called_once_with()

            inhibitor.update(enabled=True, syncing=True)
            open_inhibitor.assert_called_once_with()

            inhibitor.update(enabled=True, syncing=False)
            self.assertFalse(inhibitor.active)
            close_descriptor.assert_called_once_with(37)

    def test_release_without_descriptor_is_safe(self) -> None:
        inhibitor = ShutdownInhibitor()

        with patch("talaryn.monitor.os.close") as close_descriptor:
            inhibitor.release()

        close_descriptor.assert_not_called()

    def test_monitor_service_is_enabled_on_first_start(self) -> None:
        completed = Mock(returncode=0)
        with (
            patch(
                "talaryn.monitor.read_sync_snapshot",
                return_value=None,
            ),
            patch(
                "talaryn.monitor.read_nautilus_status_snapshot",
                return_value=None,
            ),
            patch(
                "talaryn.monitor.subprocess.run",
                return_value=completed,
            ) as run,
            patch("talaryn.monitor.subprocess.Popen") as fallback,
        ):
            ensure_monitor_running()

        run.assert_called_once_with(
            [
                "systemctl",
                "--user",
                "enable",
                "--now",
                "talaryn-monitor.service",
            ],
            stdout=ANY,
            stderr=ANY,
            check=False,
            timeout=2,
        )
        fallback.assert_not_called()

    def test_monitor_is_restarted_when_lightweight_status_is_missing(self) -> None:
        completed = Mock(returncode=0)
        with (
            patch(
                "talaryn.monitor.read_sync_snapshot",
                return_value={"updated_at": 1},
            ),
            patch(
                "talaryn.monitor.read_nautilus_status_snapshot",
                return_value=None,
            ),
            patch(
                "talaryn.monitor.subprocess.run",
                return_value=completed,
            ) as run,
            patch("talaryn.monitor.subprocess.Popen") as fallback,
        ):
            ensure_monitor_running()

        self.assertEqual(run.call_count, 3)
        self.assertEqual(
            run.call_args_list[0].args[0],
            ["systemctl", "--user", "daemon-reload"],
        )
        self.assertEqual(
            run.call_args_list[1].args[0],
            [
                "systemctl",
                "--user",
                "restart",
                "talaryn-monitor.service",
            ],
        )
        self.assertEqual(
            run.call_args_list[2].args[0],
            [
                "systemctl",
                "--user",
                "enable",
                "--now",
                "talaryn-monitor.service",
            ],
        )
        fallback.assert_not_called()

    def test_fresh_full_and_nautilus_snapshots_need_no_service_call(self) -> None:
        with (
            patch(
                "talaryn.monitor.read_sync_snapshot",
                return_value={"updated_at": 1},
            ),
            patch(
                "talaryn.monitor.read_nautilus_status_snapshot",
                return_value={"schema": 1, "updated_at": 1},
            ),
            patch("talaryn.monitor.subprocess.run") as run,
            patch("talaryn.monitor.subprocess.Popen") as fallback,
        ):
            ensure_monitor_running()

        run.assert_not_called()
        fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
