from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from talaryn.activity_store import ActivityStore
from talaryn.activity_grouping import activity_group_key


class ActivityStoreTests(unittest.TestCase):
    def test_history_is_persistent_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "activity.db"
            store = ActivityStore(path)
            event = {
                "event_id": "profile:copy:file:1",
                "profile_id": "profile",
                "timestamp": time.time(),
                "operation": "copy",
                "state": "completed",
                "path": "file.txt",
                "size": 1024,
            }
            self.assertEqual(store.save_events([event]), [event])
            self.assertEqual(store.save_events([event]), [])

            reopened = ActivityStore(path)
            self.assertEqual(reopened.recent(10)[0]["path"], "file.txt")
            self.assertEqual(reopened.summary()["day"]["bytes"], 1024)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_speed_history_discards_old_samples(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ActivityStore(Path(temp_dir) / "activity.db")
            now = time.time()
            store.record_speed(now - 90000, 1, 2)
            store.record_speed(now, 10, 20)
            history = store.speed_history(300)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["upload"], 10)
        self.assertEqual(history[0]["download"], 20)

    def test_summary_can_be_filtered_by_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ActivityStore(Path(temp_dir) / "activity.db")
            now = time.time()
            store.save_events(
                [
                    {
                        "event_id": "first:copy:file.txt:1",
                        "profile_id": "first",
                        "timestamp": now,
                        "operation": "copy",
                        "state": "completed",
                        "path": "file.txt",
                        "size": 1024,
                    },
                    {
                        "event_id": "second:copy:other.txt:2",
                        "profile_id": "second",
                        "timestamp": now,
                        "operation": "copy",
                        "state": "completed",
                        "path": "other.txt",
                        "size": 2048,
                    },
                    {
                        "event_id": "second:copy:failed.txt:3",
                        "profile_id": "second",
                        "timestamp": now,
                        "operation": "copy",
                        "state": "error",
                        "error": "failed",
                        "path": "failed.txt",
                        "size": 4096,
                    },
                ]
            )

            all_profiles = store.summary()
            first_profile = store.summary("first")
            second_profile = store.summary("second")

        self.assertEqual(all_profiles["day"]["bytes"], 3072)
        self.assertEqual(all_profiles["day"]["completed"], 2)
        self.assertEqual(all_profiles["day"]["errors"], 1)
        self.assertEqual(first_profile["day"]["bytes"], 1024)
        self.assertEqual(first_profile["day"]["completed"], 1)
        self.assertEqual(first_profile["day"]["errors"], 0)
        self.assertEqual(second_profile["day"]["bytes"], 2048)
        self.assertEqual(second_profile["day"]["completed"], 1)
        self.assertEqual(second_profile["day"]["errors"], 1)

    def test_history_can_be_read_in_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ActivityStore(Path(temp_dir) / "activity.db")
            now = time.time()
            store.save_events(
                [
                    {
                        "event_id": f"profile:copy:{name}:{timestamp}",
                        "profile_id": "profile",
                        "timestamp": timestamp,
                        "operation": "copy",
                        "state": "completed",
                        "path": name,
                    }
                    for timestamp, name in (
                        (now - 2, "old.txt"),
                        (now - 1, "middle.txt"),
                        (now, "new.txt"),
                    )
                ]
            )

            first_page = store.recent(2)
            second_page = store.recent(2, offset=2)
            history_count = store.activity_count()
            profile_history_count = store.activity_count("profile")
            missing_profile_count = store.activity_count("missing")

        self.assertEqual(history_count, 3)
        self.assertEqual(profile_history_count, 3)
        self.assertEqual(missing_profile_count, 0)
        self.assertEqual(
            [item["path"] for item in first_page],
            ["new.txt", "middle.txt"],
        )
        self.assertEqual(
            [item["path"] for item in second_page],
            ["old.txt"],
        )

    def test_group_summary_is_not_limited_to_the_loaded_page(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ActivityStore(Path(temp_dir) / "activity.db")
            group_timestamp = time.time()
            events = [
                {
                    "event_id": f"profile:copy:folder/file-{index}:1",
                    "profile_id": "profile",
                    "timestamp": group_timestamp + index / 1000,
                    "group_timestamp": group_timestamp,
                    "operation": "copy",
                    "state": "completed",
                    "path": f"folder/file-{index}.bin",
                    "size": 4096,
                }
                for index in range(300)
            ]
            store.save_events(events)

            page = store.recent(250)
            key = activity_group_key(page[0])
            summary = store.group_summaries([key])[key]

        self.assertEqual(len(page), 250)
        self.assertEqual(summary["item_count"], 300)
        self.assertEqual(summary["completed_count"], 300)
        self.assertEqual(summary["total_size"], 300 * 4096)
        self.assertEqual(summary["transferred_size"], 300 * 4096)
        self.assertEqual(summary["operation"], "copy")

    def test_recent_group_headers_have_an_independent_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ActivityStore(Path(temp_dir) / "activity.db")
            start = time.time() - 12 * 300
            events = [
                {
                    "event_id": f"profile:copy:folder-{index}/file.bin:1",
                    "profile_id": "profile",
                    "timestamp": start + index * 300,
                    "group_timestamp": start + index * 300,
                    "operation": "copy",
                    "state": "completed",
                    "path": f"folder-{index}/file.bin",
                    "size": index + 1,
                }
                for index in range(12)
            ]
            store.save_events(events)

            headers = store.recent_group_headers(10)

        self.assertEqual(len(headers), 10)
        self.assertEqual(
            headers[0]["representative"]["path"],
            "folder-11/file.bin",
        )
        self.assertEqual(headers[0]["item_count"], 1)
        self.assertEqual(
            headers[-1]["representative"]["path"],
            "folder-2/file.bin",
        )

    def test_existing_history_is_backfilled_with_group_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "activity.db"
            event = {
                "event_id": "profile:copy:folder/file.bin:1",
                "profile_id": "profile",
                "timestamp": 310.0,
                "group_timestamp": 305.0,
                "operation": "copy",
                "state": "completed",
                "path": "folder/file.bin",
                "size": 1024,
            }
            with sqlite3.connect(path) as connection:
                connection.execute(
                    """
                    CREATE TABLE activities (
                        event_id TEXT PRIMARY KEY,
                        profile_id TEXT NOT NULL,
                        timestamp REAL NOT NULL,
                        operation TEXT NOT NULL,
                        state TEXT NOT NULL,
                        error TEXT NOT NULL DEFAULT '',
                        size INTEGER NOT NULL DEFAULT 0,
                        data_json TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO activities (
                        event_id, profile_id, timestamp, operation,
                        state, error, size, data_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event["event_id"],
                        event["profile_id"],
                        event["timestamp"],
                        event["operation"],
                        event["state"],
                        "",
                        event["size"],
                        json.dumps(event),
                    ),
                )

            store = ActivityStore(path)
            key = activity_group_key(event)
            summary = store.group_summaries([key])[key]

        self.assertEqual(summary["item_count"], 1)
        self.assertEqual(summary["total_size"], 1024)
        self.assertEqual(summary["timestamp"], 305.0)

    def test_history_prunes_entries_older_than_retention_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ActivityStore(Path(temp_dir) / "activity.db")
            now = time.time()
            store.save_events(
                [
                    {
                        "event_id": "profile:copy:expired.txt:1",
                        "profile_id": "profile",
                        "timestamp": now - 366 * 86400,
                        "operation": "copy",
                        "state": "completed",
                        "path": "expired.txt",
                    },
                    {
                        "event_id": "profile:copy:current.txt:2",
                        "profile_id": "profile",
                        "timestamp": now,
                        "operation": "copy",
                        "state": "completed",
                        "path": "current.txt",
                    },
                ]
            )

            self.assertEqual(store.activity_count(), 1)
            self.assertEqual(store.recent(10)[0]["path"], "current.txt")

    def test_clear_history_does_not_restore_old_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ActivityStore(Path(temp_dir) / "activity.db")
            old_event = {
                "event_id": "profile:copy:old.txt:1",
                "profile_id": "profile",
                "timestamp": time.time() - 60,
                "operation": "copy",
                "state": "completed",
                "path": "old.txt",
                "size": 1024,
            }
            store.save_events([old_event])
            store.record_speed(time.time(), 10, 20)

            self.assertEqual(store.clear_history(), 1)
            self.assertEqual(store.recent(10), [])
            self.assertEqual(store.summary()["day"]["completed"], 0)
            self.assertEqual(len(store.speed_history(300)), 1)
            self.assertEqual(store.save_events([old_event]), [])

            store = ActivityStore(Path(temp_dir) / "activity.db")
            self.assertEqual(store.save_events([old_event]), [])

            new_event = {
                **old_event,
                "event_id": "profile:copy:new.txt:2",
                "timestamp": time.time() + 1,
                "path": "new.txt",
            }
            self.assertEqual(store.save_events([new_event]), [new_event])
            self.assertEqual(store.recent(10)[0]["path"], "new.txt")

    def test_modified_event_replaces_copy_for_the_same_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ActivityStore(Path(temp_dir) / "activity.db")
            timestamp = time.time()
            base = {
                "profile_id": "profile",
                "timestamp": timestamp,
                "state": "completed",
                "path": "folder/file.txt",
                "size": 10,
            }
            copied = {
                **base,
                "event_id": (
                    f"profile:copy:folder/file.txt:{int(timestamp)}"
                ),
                "operation": "copy",
            }
            modified = {
                **base,
                "event_id": (
                    f"profile:modify:folder/file.txt:{int(timestamp)}"
                ),
                "operation": "modify",
            }

            store.save_events([copied])
            store.save_events([modified])
            recent = store.recent(10)

        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["operation"], "modify")

    def test_zero_size_history_is_backfilled_from_queue_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "activity.db"
            store = ActivityStore(path)
            store.save_events(
                [
                    {
                        "event_id": "profile:copy:folder/file.bin:1",
                        "profile_id": "profile",
                        "timestamp": time.time(),
                        "operation": "copy",
                        "state": "completed",
                        "path": "folder/file.bin",
                        "size": 0,
                    }
                ]
            )
            store.observe_queue(
                "profile",
                123,
                [
                    {
                        "queue_id": 7,
                        "path": "folder/file.bin",
                        "size": 4096,
                    }
                ],
                time.time(),
            )

            reopened = ActivityStore(path)
            self.assertEqual(reopened.recent(10)[0]["size"], 4096)
            self.assertEqual(reopened.summary()["day"]["bytes"], 4096)

    def test_incremental_log_read_keeps_short_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ActivityStore(root / "activity.db")
            log_file = root / "profile.log"
            replaced = (
                "2026/07/24 12:02:00 INFO  : folder/file.txt: "
                "Copied (replaced existing)\n"
            )
            succeeded = (
                "2026/07/24 12:02:00 INFO  : folder/file.txt: "
                "vfs cache: upload succeeded try #1\n"
            )
            log_file.write_text(replaced, encoding="utf-8")
            self.assertEqual(store.read_new_log(log_file, 4096), replaced)
            with log_file.open("a", encoding="utf-8") as stream:
                stream.write(succeeded)
            second_read = store.read_new_log(log_file, 4096)

        self.assertIn("Copied (replaced existing)", second_read)
        self.assertIn("upload succeeded", second_read)

    def test_incremental_log_read_keeps_unread_rotated_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ActivityStore(root / "activity.db")
            log_file = root / "profile.log"
            first = "2026/08/01 12:00:00 INFO  : first line\n"
            pending = "2026/08/01 12:00:01 INFO  : pending line\n"
            current = "2026/08/01 12:00:02 INFO  : current line\n"
            log_file.write_text(first, encoding="utf-8")
            self.assertEqual(store.read_new_log(log_file, 4096), first)
            with log_file.open("a", encoding="utf-8") as stream:
                stream.write(pending)
            log_file.rename(
                root / "profile-2026-08-01T12-00-01.000.log"
            )
            log_file.write_text(current, encoding="utf-8")

            after_rotation = store.read_new_log(log_file, 4096)

        self.assertIn("pending line", after_rotation)
        self.assertIn("current line", after_rotation)

    def test_queue_item_keeps_first_seen_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ActivityStore(Path(temp_dir) / "activity.db")
            first = {
                "queue_id": 7,
                "path": "folder/file.txt",
                "timestamp": 100.0,
            }
            store.observe_queue("profile", 123, [first], 100.0)
            second = {
                "queue_id": 7,
                "path": "folder/file.txt",
                "timestamp": 160.0,
            }
            store.observe_queue("profile", 123, [second], 160.0)
            observed = store.observed_upload(
                "profile",
                123,
                "folder/file.txt",
                160.0,
            )

        self.assertEqual(first["first_seen"], 100.0)
        self.assertEqual(second["timestamp"], 100.0)
        self.assertEqual(second["group_timestamp"], 100.0)
        self.assertIsNotNone(observed)
        self.assertEqual(observed["first_seen"], 100.0)


if __name__ == "__main__":
    unittest.main()
