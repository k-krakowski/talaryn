from __future__ import annotations

import tempfile
import time
import unittest
import json
import stat
from pathlib import Path
from unittest.mock import patch

from talaryn.activity_store import ActivityStore
from talaryn.sync_status import (
    add_activity_group_headers,
    activity_display_path,
    activity_group_key,
    group_activity_events,
    _history_event,
    _mark_active_items,
    _nautilus_status_payload,
    _rc_url,
    collect_sync_activity,
    get_sync_activity,
    merge_activity_group_summaries,
    metadata_relative_path,
    write_sync_snapshot,
)


class SyncStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile_id = "example"
        self.profile = {
            "label": "Example drive",
            "mount_dir": "/mnt/example",
            "remote_name": "remote",
            "remote_path": "",
            "remote_spec": "remote:",
        }

    def test_rc_url_remains_compatible_with_old_logs(self) -> None:
        log = (
            "NOTICE: Serving remote control on http://127.0.0.1:40101/\n"
            "NOTICE: Serving remote control on http://127.0.0.1:40202/\n"
        )
        self.assertEqual(_rc_url(log), "http://127.0.0.1:40202/")

    def test_gui_snapshot_reader_never_runs_a_second_collector(self) -> None:
        with (
            patch(
                "talaryn.sync_status.read_sync_snapshot",
                return_value=None,
            ),
            patch(
                "talaryn.sync_status.collect_sync_activity"
            ) as collector,
        ):
            activity = get_sync_activity(
                {self.profile_id: self.profile}
            )

        collector.assert_not_called()
        self.assertFalse(activity["monitor_available"])
        self.assertFalse(activity["is_syncing"])
        self.assertIn(self.profile_id, activity["profiles"])

    def test_nautilus_snapshot_contains_only_live_emblem_state(self) -> None:
        activity = {
            "updated_at": 10.0,
            "profiles": {
                "connected": {
                    "connected": True,
                    "quota": {"total": 100},
                },
                "offline": {"connected": False},
            },
            "syncing_paths": {"connected": ["folder/file.txt"]},
            "recent": [{"path": "large-history-entry"}],
            "events": [{"path": "another-large-entry"}],
        }

        with patch("talaryn.sync_status.time.time", return_value=20.0):
            payload = _nautilus_status_payload(activity)

        self.assertEqual(payload["updated_at"], 20.0)
        self.assertEqual(payload["schema"], 1)
        self.assertEqual(
            payload["profiles"],
            {
                "connected": {"connected": True},
                "offline": {"connected": False},
            },
        )
        self.assertEqual(
            payload["syncing_paths"],
            {"connected": ["folder/file.txt"]},
        )
        self.assertNotIn("recent", payload)
        self.assertNotIn("events", payload)

    def test_snapshot_writer_creates_private_full_and_nautilus_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            full_path = root / "sync-status.json"
            nautilus_path = root / "nautilus-status.json"
            activity = {
                "updated_at": 10.0,
                "profiles": {"drive": {"connected": True, "quota": {}}},
                "syncing_paths": {"drive": ["file.txt"]},
                "recent": [{"path": "history.txt"}],
                "new_events": [{"path": "private-runtime-only.txt"}],
            }
            with (
                patch(
                    "talaryn.sync_status.SYNC_SNAPSHOT_FILE",
                    full_path,
                ),
                patch(
                    "talaryn.sync_status.NAUTILUS_STATUS_FILE",
                    nautilus_path,
                ),
                patch("talaryn.sync_status.ensure_runtime_dirs"),
                patch("talaryn.sync_status.time.time", return_value=20.0),
            ):
                write_sync_snapshot(activity)

            full = json.loads(full_path.read_text(encoding="utf-8"))
            lightweight = json.loads(
                nautilus_path.read_text(encoding="utf-8")
            )
            full_mode = stat.S_IMODE(full_path.stat().st_mode)
            nautilus_mode = stat.S_IMODE(nautilus_path.stat().st_mode)

        self.assertIn("recent", full)
        self.assertNotIn("new_events", full)
        self.assertEqual(lightweight["schema"], 1)
        self.assertNotIn("recent", lightweight)
        self.assertEqual(full_mode, 0o600)
        self.assertEqual(nautilus_mode, 0o600)

    def test_metadata_path_strips_cache_remote_and_remote_subpath(self) -> None:
        root = Path("/cache/profile/vfsMeta")
        profile = {"remote_path": "team/files"}
        metadata = root / "drive{fingerprint}" / "team/files/report.txt"
        self.assertEqual(
            metadata_relative_path(metadata, root, profile),
            Path("report.txt"),
        )

    def _collect(
        self,
        root: Path,
        responses: dict[str, dict | None],
        log: str = "",
    ) -> dict:
        log_file = root / "example.log"
        log_file.write_text(log, encoding="utf-8")
        store = ActivityStore(root / "activity.db")

        def rc_response(
            _profile_id: str,
            method: str,
            _params: dict | None = None,
            **_kwargs: object,
        ) -> dict | None:
            if method == "core/version":
                return responses.get(method, {"decomposed": [1, 74, 4]})
            if method == "core/pid":
                return responses.get(method, {"pid": 123})
            return responses.get(method)

        with (
            patch(
                "talaryn.sync_status.log_file_for",
                return_value=log_file,
            ),
            patch(
                "talaryn.sync_status.rc_call",
                side_effect=rc_response,
            ),
            patch.dict("talaryn.sync_status._ABOUT_CACHE", {}, clear=True),
        ):
            return collect_sync_activity(
                {self.profile_id: self.profile},
                store=store,
            )

    def test_vfs_queue_is_authoritative_and_success_log_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            activity = self._collect(
                Path(temp_dir),
                {
                    "vfs/queue": {
                        "queue": [
                            {
                                "id": 7,
                                "name": "waiting.bin",
                                "size": 1024,
                                "expiry": 5,
                                "tries": 0,
                                "delay": 0,
                                "uploading": False,
                            }
                        ]
                    },
                    "core/stats": {"transferring": []},
                    "vfs/stats": {
                        "diskCache": {
                            "bytesUsed": 1024,
                            "files": 1,
                            "uploadsQueued": 1,
                        }
                    },
                    "core/transferred": {"transferred": []},
                },
                "2026/07/24 12:01:00 INFO  : done.txt: "
                "vfs cache: upload succeeded try #1\n",
            )

        self.assertEqual(activity["queued_count"], 1)
        self.assertEqual(activity["queued"][0]["path"], "waiting.bin")
        self.assertEqual(activity["queued"][0]["queue_id"], 7)
        self.assertEqual(activity["recent"][0]["path"], "done.txt")
        self.assertEqual(activity["history_total"], 1)

    def test_internal_uploads_still_block_safe_unmount(self):
        with tempfile.TemporaryDirectory() as directory:
            activity = self._collect(Path(directory), {
                "vfs/queue": {"queue": [{"name": ".goutputstream-123", "uploading": True}]},
                "vfs/stats": {"diskCache": {"uploadsQueued": 0, "uploadsInProgress": 1, "erroredFiles": 0}},
                "core/stats": {},
            }, "")
        self.assertEqual(activity["active"], [])
        self.assertTrue(activity["is_syncing"])
        self.assertEqual(activity["profiles"][self.profile_id]["transfer_state"], "busy")

    def test_rc_failure_cannot_be_reported_as_safe_to_unmount(self):
        with tempfile.TemporaryDirectory() as directory:
            activity = self._collect(Path(directory), {"core/pid": None}, "")
        self.assertEqual(activity["profiles"][self.profile_id]["transfer_state"], "unknown")

    def test_core_stats_add_progress_to_matching_queue_file(self) -> None:
        items = [{"path": "folder/video.mp4", "state": "queued"}]
        _mark_active_items(
            items,
            {
                "transferring": [
                    {
                        "name": "folder/video.mp4",
                        "bytes": 50,
                        "percentage": 25,
                        "speedAvg": 10,
                    }
                ]
            },
        )
        self.assertEqual(items[0]["state"], "uploading")
        self.assertEqual(items[0]["progress"], 25)

    def test_activity_groups_use_profile_folder_and_time_bucket(self) -> None:
        base = {
            "profile_id": "example",
            "profile_label": "Example drive",
            "profile_icon": "folder-remote.svg",
            "mount_dir": "/mnt/example",
            "operation": "copy",
            "state": "completed",
            "size": 10,
        }
        events = [
            {
                **base,
                "event_id": "one",
                "path": "folder/one.txt",
                "timestamp": 310.0,
            },
            {
                **base,
                "event_id": "two",
                "path": "folder/two.txt",
                "timestamp": 350.0,
            },
            {
                **base,
                "event_id": "other-folder",
                "path": "other/three.txt",
                "timestamp": 350.0,
            },
            {
                **base,
                "event_id": "later",
                "path": "folder/later.txt",
                "timestamp": 610.0,
            },
        ]

        groups = group_activity_events(events)

        self.assertEqual(len(groups), 3)
        folder_groups = [
            group
            for group in groups
            if group["folder_path"] == "/mnt/example/folder"
        ]
        self.assertEqual(
            sorted(group["item_count"] for group in folder_groups),
            [1, 2],
        )
        self.assertTrue(
            all(
                group["display_folder_path"] == "example/folder"
                for group in folder_groups
            )
        )
        self.assertTrue(
            all(group["operation"] == "copy" for group in groups)
        )

    def test_complete_group_summary_keeps_children_paginated(self) -> None:
        base = {
            "profile_id": "example",
            "profile_label": "Example drive",
            "profile_icon": "folder-remote.svg",
            "mount_dir": "/mnt/example",
            "operation": "copy",
            "state": "completed",
            "group_timestamp": 310.0,
            "size": 10,
        }
        events = [
            {
                **base,
                "event_id": f"loaded-{index}",
                "path": f"folder/file-{index}.txt",
                "timestamp": 400.0 + index,
            }
            for index in range(250)
        ]
        groups = group_activity_events(events)
        key = groups[0]["group_key"]

        merge_activity_group_summaries(
            groups,
            {
                key: {
                    "item_count": 796,
                    "active_count": 0,
                    "queued_count": 0,
                    "completed_count": 796,
                    "error_count": 0,
                    "operation": "copy",
                    "total_size": 3_300_000_000,
                    "transferred_size": 3_300_000_000,
                    "timestamp": 310.0,
                }
            },
        )

        self.assertEqual(groups[0]["item_count"], 796)
        self.assertEqual(groups[0]["completed_count"], 796)
        self.assertEqual(groups[0]["total_size"], 3_300_000_000)
        self.assertEqual(len(groups[0]["items"]), 250)

    def test_summary_only_headers_do_not_load_child_items(self) -> None:
        event = {
            "event_id": "older",
            "profile_id": "example",
            "profile_label": "Example drive",
            "profile_icon": "folder-remote.svg",
            "mount_dir": "/mnt/example",
            "operation": "copy",
            "state": "completed",
            "group_timestamp": 310.0,
            "timestamp": 400.0,
            "path": "older/file.txt",
            "size": 10,
        }
        header = {
            "group_key": activity_group_key(event),
            "representative": event,
        }

        groups = add_activity_group_headers([], [header])

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["group_key"], header["group_key"])
        self.assertEqual(groups[0]["items"], [])

    def test_activity_display_path_starts_at_mount_folder(self) -> None:
        self.assertEqual(
            activity_display_path(
                {
                    "profile_id": "onedrive",
                    "mount_dir": (
                        "/home/user/cloud/onedrive-priv"
                    ),
                    "path": "test/test2/file222",
                }
            ),
            "onedrive-priv/test/test2/file222",
        )

    def test_history_event_keeps_local_file_size(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "file.txt"
            path.write_bytes(b"content")
            event = _history_event(
                self.profile_id,
                self.profile,
                "copy",
                "file.txt",
                time.time(),
                str(path),
                "remote:file.txt",
                str(path),
                str(path),
            )

        self.assertEqual(event["size"], 7)
        self.assertTrue(event["size_known"])

    def test_activity_group_uses_changes_only_for_mixed_operations(self) -> None:
        base = {
            "profile_id": "example",
            "mount_dir": "/mnt/example",
            "profile_label": "Example",
            "profile_icon": "folder-remote.svg",
            "timestamp": 310.0,
            "state": "completed",
            "size": 0,
        }
        deleted = group_activity_events(
            [
                {
                    **base,
                    "event_id": "delete-one",
                    "path": "folder/one.txt",
                    "operation": "delete",
                },
                {
                    **base,
                    "event_id": "delete-two",
                    "path": "folder/two.txt",
                    "operation": "delete",
                },
            ]
        )
        mixed = group_activity_events(
            [
                {
                    **base,
                    "event_id": "rename",
                    "path": "folder/one.txt",
                    "operation": "rename",
                },
                {
                    **base,
                    "event_id": "delete",
                    "path": "folder/two.txt",
                    "operation": "delete",
                },
            ]
        )

        self.assertEqual(deleted[0]["operation"], "delete")
        self.assertEqual(mixed[0]["operation"], "mixed")

    def test_active_and_completed_versions_keep_the_same_group(self) -> None:
        base = {
            "profile_id": "example",
            "mount_dir": "/mnt/example",
            "path": "folder/file.txt",
            "group_timestamp": 310.0,
        }
        active = {
            **base,
            "event_id": "queue",
            "timestamp": 310.0,
            "state": "uploading",
        }
        completed = {
            **base,
            "event_id": "completed",
            "timestamp": 480.0,
            "state": "completed",
        }

        self.assertEqual(
            activity_group_key(active),
            activity_group_key(completed),
        )

    def test_history_distinguishes_copy_rename_and_delete(self) -> None:
        log = "\n".join(
            (
                "2026/07/24 12:01:00 INFO  : copied.txt: "
                "vfs cache: upload succeeded try #1",
                "2026/07/24 12:02:00 INFO  : folder/old.txt: "
                'vfs cache: renamed in cache to "folder/new.txt"',
                "2026/07/24 12:03:00 INFO  : deleted.txt: "
                "Moved (server-side) to: .Trash-1000/files/deleted.txt",
            )
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            activity = self._collect(Path(temp_dir), {}, log)

        by_operation = {
            event["operation"]: event for event in activity["recent"]
        }
        self.assertEqual(set(by_operation), {"copy", "rename", "delete"})
        self.assertEqual(
            by_operation["copy"]["destination"],
            "remote:copied.txt",
        )
        self.assertEqual(
            by_operation["rename"]["source"],
            "/mnt/example/folder/old.txt",
        )
        self.assertEqual(by_operation["delete"]["destination"], "")

    def test_internal_relocation_is_shown_as_copy(self) -> None:
        log = (
            "2026/07/24 12:02:00 INFO  : source/file.txt: "
            'vfs cache: renamed in cache to "target/file.txt"\n'
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            activity = self._collect(Path(temp_dir), {}, log)
        event = activity["recent"][0]
        self.assertEqual(event["operation"], "copy")
        self.assertEqual(event["source_icon"], "remote-storage.svg")
        self.assertEqual(event["destination_icon"], "remote-storage.svg")

    def test_replaced_existing_upload_is_shown_as_modified(self) -> None:
        log = "\n".join(
            (
                "2026/07/24 12:02:00 INFO  : folder/file.txt: "
                "Copied (replaced existing)",
                "2026/07/24 12:02:00 INFO  : folder/file.txt: "
                "vfs cache: upload succeeded try #1",
            )
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            activity = self._collect(Path(temp_dir), {}, log)

        self.assertEqual(len(activity["recent"]), 1)
        event = activity["recent"][0]
        self.assertEqual(event["operation"], "modify")
        self.assertEqual(event["source"], "/mnt/example/folder/file.txt")
        self.assertEqual(event["destination"], "remote:folder/file.txt")

    def test_new_upload_remains_copy(self) -> None:
        log = "\n".join(
            (
                "2026/07/24 12:02:00 INFO  : folder/file.txt: Copied (new)",
                "2026/07/24 12:02:00 INFO  : folder/file.txt: "
                "vfs cache: upload succeeded try #1",
            )
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            activity = self._collect(Path(temp_dir), {}, log)

        self.assertEqual(activity["recent"][0]["operation"], "copy")

    def test_log_remote_prefix_and_profile_root_are_removed(self) -> None:
        profile = dict(
            self.profile,
            remote_name="gdrive-uni",
            remote_path="test",
            remote_spec="gdrive-uni:test",
        )
        log = (
            "2026/07/24 12:02:00 INFO  : "
            "gdrive-uni{fingerprint}:test/folder/file.txt: "
            "vfs cache: upload succeeded try #1\n"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            log_file = root / "example.log"
            log_file.write_text(log, encoding="utf-8")
            store = ActivityStore(root / "activity.db")
            with (
                patch(
                    "talaryn.sync_status.log_file_for",
                    return_value=log_file,
                ),
                patch(
                    "talaryn.sync_status.rc_call",
                    return_value=None,
                ),
            ):
                activity = collect_sync_activity(
                    {self.profile_id: profile},
                    store=store,
                )
        self.assertEqual(activity["recent"][0]["path"], "folder/file.txt")

    def test_background_download_contributes_speed_but_not_activity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            activity = self._collect(
                Path(temp_dir),
                {
                    "vfs/queue": {
                        "queue": [
                            {
                                "id": 8,
                                "name": "upload.bin",
                                "size": 100,
                                "expiry": -1,
                                "tries": 0,
                                "uploading": True,
                            }
                        ]
                    },
                    "core/stats": {
                        "transferring": [
                            {
                                "name": "upload.bin",
                                "bytes": 50,
                                "percentage": 50,
                                "speedAvg": 100,
                            },
                            {
                                "name": "download.bin",
                                "bytes": 25,
                                "percentage": 25,
                                "speedAvg": 200,
                            },
                        ]
                    },
                    "vfs/stats": {"diskCache": {}},
                    "core/transferred": {"transferred": []},
                },
            )

        self.assertEqual(activity["active_count"], 1)
        self.assertEqual(activity["downloading_count"], 1)
        self.assertEqual(activity["upload_speed"], 100)
        self.assertEqual(activity["download_speed"], 200)
        self.assertEqual(activity["downloads"], [])
        self.assertEqual(
            [item["path"] for item in activity["events"]],
            ["upload.bin"],
        )

    def test_completed_transfer_is_kept_only_if_it_was_observed_in_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = self._collect(
                root,
                {
                    "vfs/queue": {
                        "queue": [
                            {
                                "id": 9,
                                "name": "report.pdf",
                                "size": 123,
                                "expiry": -1,
                                "tries": 0,
                                "uploading": True,
                            }
                        ]
                    },
                    "core/stats": {"transferring": []},
                    "vfs/stats": {"diskCache": {}},
                    "core/transferred": {"transferred": []},
                },
            )
            self.assertEqual(first["active_count"], 1)
            first_seen = first["active"][0]["group_timestamp"]

            store = ActivityStore(root / "activity.db")
            log_file = root / "example.log"
            timestamp = time.time()

            def rc_response(
                _profile_id: str,
                method: str,
                _params: dict | None = None,
                **_kwargs: object,
            ) -> dict | None:
                return {
                    "core/version": {"decomposed": [1, 74, 4]},
                    "core/pid": {"pid": 123},
                    "vfs/queue": {"queue": []},
                    "core/stats": {"transferring": []},
                    "vfs/stats": {"diskCache": {}},
                    "core/transferred": {
                        "transferred": [
                            {
                                "name": "report.pdf",
                                "size": 123,
                                "bytes": 123,
                                "what": "transferring",
                                "timestamp": int(timestamp * 1000),
                                "error": "",
                                "jobid": 0,
                            },
                            {
                                "name": "preview.jpg",
                                "size": 10,
                                "bytes": 10,
                                "what": "transferring",
                                "timestamp": int(timestamp * 1000),
                                "error": "",
                                "jobid": 0,
                            },
                        ]
                    },
                    "operations/about": {},
                }.get(method)

            with (
                patch(
                    "talaryn.sync_status.log_file_for",
                    return_value=log_file,
                ),
                patch(
                    "talaryn.sync_status.rc_call",
                    side_effect=rc_response,
                ),
                patch.dict(
                    "talaryn.sync_status._ABOUT_CACHE", {}, clear=True
                ),
            ):
                second = collect_sync_activity(
                    {self.profile_id: self.profile},
                    store=store,
                )

        paths = [event["path"] for event in second["recent"]]
        self.assertIn("report.pdf", paths)
        self.assertNotIn("preview.jpg", paths)
        report = next(
            event
            for event in second["recent"]
            if event["path"] == "report.pdf"
        )
        self.assertEqual(report["group_timestamp"], first_seen)

    def test_upload_log_uses_size_from_observed_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._collect(
                root,
                {
                    "vfs/queue": {
                        "queue": [
                            {
                                "id": 9,
                                "name": "report.pdf",
                                "size": 4096,
                                "expiry": -1,
                                "tries": 0,
                                "uploading": True,
                            }
                        ]
                    },
                    "core/stats": {"transferring": []},
                    "vfs/stats": {"diskCache": {}},
                    "core/transferred": {"transferred": []},
                },
            )

            store = ActivityStore(root / "activity.db")
            log_file = root / "example.log"
            timestamp = time.time()
            log_file.write_text(
                time.strftime(
                    "%Y/%m/%d %H:%M:%S",
                    time.localtime(timestamp),
                )
                + " INFO  : report.pdf: "
                "vfs cache: upload succeeded try #1\n",
                encoding="utf-8",
            )

            def rc_response(
                _profile_id: str,
                method: str,
                _params: dict | None = None,
                **_kwargs: object,
            ) -> dict | None:
                return {
                    "core/version": {"decomposed": [1, 74, 4]},
                    "core/pid": {"pid": 123},
                    "vfs/queue": {"queue": []},
                    "core/stats": {"transferring": []},
                    "vfs/stats": {"diskCache": {}},
                    "core/transferred": {"transferred": []},
                    "operations/about": {},
                }.get(method)

            with (
                patch(
                    "talaryn.sync_status.log_file_for",
                    return_value=log_file,
                ),
                patch(
                    "talaryn.sync_status.rc_call",
                    side_effect=rc_response,
                ),
                patch.dict(
                    "talaryn.sync_status._ABOUT_CACHE",
                    {},
                    clear=True,
                ),
            ):
                activity = collect_sync_activity(
                    {self.profile_id: self.profile},
                    store=store,
                )

        self.assertEqual(activity["recent"][0]["size"], 4096)
        self.assertEqual(activity["history_summary"]["day"]["bytes"], 4096)


if __name__ == "__main__":
    unittest.main()
