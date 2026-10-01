from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from .activity_filter import activity_is_visible
from .activity_grouping import activity_group_key, activity_group_timestamp
from .paths import ACTIVITY_DB, ensure_runtime_dirs

ACTIVITY_RETENTION_SECONDS = 365 * 86400
MAX_ACTIVITY_EVENTS = 100_000
PRUNE_INTERVAL_SECONDS = 3600


class ActivityStore:
    def __init__(self, path: Path = ACTIVITY_DB) -> None:
        if path == ACTIVITY_DB:
            ensure_runtime_dirs()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._initialize()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _connect(self, hidden_patterns: tuple[str, ...] = ()) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=2.0)
        if hidden_patterns:
            def visible(data: str) -> bool:
                try:
                    item = json.loads(data)
                except (TypeError, ValueError):
                    return True
                return not isinstance(item, dict) or activity_is_visible(item, hidden_patterns)

            connection.create_function("activity_visible", 1, visible, deterministic=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=2000")
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(f"{self.path}{suffix}").chmod(0o600)
            except OSError:
                pass
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS activities (
                    event_id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    operation TEXT NOT NULL,
                    state TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    size INTEGER NOT NULL DEFAULT 0,
                    group_key TEXT NOT NULL DEFAULT '',
                    group_timestamp REAL NOT NULL DEFAULT 0,
                    data_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS activities_recent
                    ON activities(timestamp DESC);
                CREATE INDEX IF NOT EXISTS activities_profile_recent
                    ON activities(profile_id, timestamp DESC);

                CREATE TABLE IF NOT EXISTS log_cursors (
                    path TEXT PRIMARY KEY,
                    inode INTEGER NOT NULL,
                    offset INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS queue_observations (
                    profile_id TEXT NOT NULL,
                    process_id INTEGER NOT NULL,
                    queue_id INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    data_json TEXT NOT NULL,
                    PRIMARY KEY (profile_id, process_id, queue_id)
                );
                CREATE INDEX IF NOT EXISTS queue_observations_path
                    ON queue_observations(profile_id, process_id, path, last_seen);

                CREATE TABLE IF NOT EXISTS transferred_seen (
                    transfer_id TEXT PRIMARY KEY,
                    timestamp REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS speed_samples (
                    timestamp REAL PRIMARY KEY,
                    upload REAL NOT NULL,
                    download REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS store_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(activities)"
                ).fetchall()
            }
            if "size" not in columns:
                connection.execute(
                    "ALTER TABLE activities "
                    "ADD COLUMN size INTEGER NOT NULL DEFAULT 0"
                )
            if "group_key" not in columns:
                connection.execute(
                    "ALTER TABLE activities "
                    "ADD COLUMN group_key TEXT NOT NULL DEFAULT ''"
                )
            if "group_timestamp" not in columns:
                connection.execute(
                    "ALTER TABLE activities "
                    "ADD COLUMN group_timestamp REAL NOT NULL DEFAULT 0"
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS activities_group
                ON activities(group_key)
                """
            )
            migration = connection.execute(
                "SELECT value FROM store_metadata WHERE key = ?",
                ("activity_size_backfill_v1",),
            ).fetchone()
            if migration is None:
                if self._backfill_activity_sizes(connection):
                    connection.execute(
                        "INSERT INTO store_metadata(key, value) VALUES (?, ?)",
                        ("activity_size_backfill_v1", "complete"),
                    )
            group_migration = connection.execute(
                "SELECT value FROM store_metadata WHERE key = ?",
                ("activity_group_backfill_v1",),
            ).fetchone()
            if group_migration is None:
                self._backfill_activity_groups(connection)
                connection.execute(
                    "INSERT INTO store_metadata(key, value) VALUES (?, ?)",
                    ("activity_group_backfill_v1", "complete"),
                )

    @staticmethod
    def _backfill_activity_groups(
        connection: sqlite3.Connection,
    ) -> None:
        rows = connection.execute(
            "SELECT event_id, data_json FROM activities"
        ).fetchall()
        for row in rows:
            try:
                event = json.loads(row["data_json"])
            except (TypeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            connection.execute(
                """
                UPDATE activities
                SET group_key = ?, group_timestamp = ?
                WHERE event_id = ?
                """,
                (
                    activity_group_key(event),
                    activity_group_timestamp(event),
                    str(row["event_id"]),
                ),
            )

    def _backfill_activity_sizes(
        self,
        connection: sqlite3.Connection,
    ) -> bool:
        """Recover sizes omitted by older log-only activity records."""
        observations: dict[
            tuple[str, str],
            list[tuple[float, float, int]],
        ] = {}
        rows = connection.execute(
            """
            SELECT profile_id, path, first_seen, last_seen, data_json
            FROM queue_observations
            ORDER BY last_seen DESC
            """
        ).fetchall()
        for row in rows:
            key = (
                str(row["profile_id"]),
                str(row["path"]).strip().strip("/"),
            )
            try:
                item = json.loads(row["data_json"])
                size = int(item.get("size", 0) or 0)
            except (AttributeError, TypeError, ValueError):
                continue
            if size > 0:
                observations.setdefault(key, []).append(
                    (
                        float(row["first_seen"]),
                        float(row["last_seen"]),
                        size,
                    )
                )
        if not observations:
            return False

        rows = connection.execute(
            """
            SELECT event_id, profile_id, data_json
            FROM activities
            WHERE size = 0
              AND operation IN ('copy', 'modify')
            """
        ).fetchall()
        for row in rows:
            try:
                event = json.loads(row["data_json"])
                path = str(event.get("path", "")).strip().strip("/")
                timestamp = float(event.get("timestamp", 0) or 0)
            except (AttributeError, TypeError, ValueError):
                continue
            candidates = [
                observation
                for observation in observations.get(
                    (str(row["profile_id"]), path),
                    [],
                )
                if observation[0] <= timestamp + 300
                and observation[1] >= timestamp - 3600
            ]
            size = (
                min(
                    candidates,
                    key=lambda observation: abs(
                        observation[1] - timestamp
                    ),
                )[2]
                if candidates
                else 0
            )
            if size <= 0:
                continue
            event["size"] = size
            event["size_known"] = True
            connection.execute(
                """
                UPDATE activities
                SET size = ?, data_json = ?
                WHERE event_id = ?
                """,
                (
                    size,
                    json.dumps(event, ensure_ascii=False, sort_keys=True),
                    str(row["event_id"]),
                ),
            )
        return True

    def save_events(self, events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        pending_events = list(events)
        if not pending_events:
            return []

        inserted: list[dict[str, Any]] = []
        with self._connect() as connection:
            # Serialize this check with clear_history() so an overlapping
            # monitor refresh cannot restore entries cleared by the user.
            connection.execute("BEGIN IMMEDIATE")
            cleared_row = connection.execute(
                """
                SELECT value
                FROM store_metadata
                WHERE key = 'history_cleared_at'
                """
            ).fetchone()
            try:
                history_cleared_at = (
                    float(cleared_row["value"]) if cleared_row is not None else 0.0
                )
            except (TypeError, ValueError):
                history_cleared_at = 0.0
            for event in pending_events:
                event_id = str(event.get("event_id", "")).strip()
                if not event_id:
                    continue
                try:
                    event_timestamp = float(event.get("timestamp", 0) or 0)
                except (TypeError, ValueError):
                    event_timestamp = 0.0
                if event_timestamp <= history_cleared_at:
                    continue
                payload = json.dumps(event, ensure_ascii=False, sort_keys=True)
                operation = str(event.get("operation", "copy"))
                group_key = activity_group_key(event)
                group_timestamp = activity_group_timestamp(event)
                if operation == "modify":
                    replaced_copy_id = (
                        f"{event.get('profile_id', '')}:copy:"
                        f"{str(event.get('path', '')).strip('/')}:"
                        f"{int(float(event.get('timestamp', 0) or 0))}"
                    )
                    connection.execute(
                        "DELETE FROM activities WHERE event_id = ?",
                        (replaced_copy_id,),
                    )
                existed = connection.execute(
                    "SELECT 1 FROM activities WHERE event_id = ?",
                    (event_id,),
                ).fetchone() is not None
                cursor = connection.execute(
                    """
                    INSERT INTO activities (
                        event_id, profile_id, timestamp, operation,
                        state, error, size, group_key, group_timestamp,
                        data_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        state = CASE
                            WHEN excluded.error != ''
                              OR excluded.size > activities.size
                            THEN excluded.state
                            ELSE activities.state
                        END,
                        error = CASE
                            WHEN excluded.error != ''
                            THEN excluded.error
                            ELSE activities.error
                        END,
                        size = MAX(activities.size, excluded.size),
                        group_key = excluded.group_key,
                        group_timestamp = excluded.group_timestamp,
                        data_json = CASE
                            WHEN excluded.error != ''
                              OR excluded.size > activities.size
                              OR excluded.group_key != activities.group_key
                            THEN excluded.data_json
                            ELSE activities.data_json
                        END
                    """,
                    (
                        event_id,
                        str(event.get("profile_id", "")),
                        float(event.get("timestamp", 0) or 0),
                        operation,
                        str(event.get("state", "completed")),
                        str(event.get("error", "")),
                        int(event.get("size", 0) or 0),
                        group_key,
                        group_timestamp,
                        payload,
                    ),
                )
                if cursor.rowcount and not existed:
                    inserted.append(event)
            self._prune_activities_if_due(connection, time.time())
        return inserted

    def _prune_activities_if_due(
        self,
        connection: sqlite3.Connection,
        now: float,
    ) -> None:
        row = connection.execute(
            "SELECT value FROM store_metadata WHERE key = ?",
            ("activities_pruned_at",),
        ).fetchone()
        try:
            last_prune = float(row["value"]) if row is not None else 0.0
        except (TypeError, ValueError):
            last_prune = 0.0
        if now - last_prune < PRUNE_INTERVAL_SECONDS:
            return

        connection.execute(
            "DELETE FROM activities WHERE timestamp < ?",
            (now - ACTIVITY_RETENTION_SECONDS,),
        )
        connection.execute(
            """
            DELETE FROM activities
            WHERE event_id IN (
                SELECT event_id
                FROM activities
                ORDER BY timestamp DESC, event_id DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (MAX_ACTIVITY_EVENTS,),
        )
        connection.execute(
            """
            INSERT INTO store_metadata(key, value)
            VALUES ('activities_pruned_at', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (repr(now),),
        )

    def recent(
        self,
        limit: int,
        profile_id: str | None = None,
        operation: str | None = None,
        state: str | None = None,
        offset: int = 0,
        hidden_patterns: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        values: list[Any] = []
        if profile_id:
            conditions.append("profile_id = ?")
            values.append(profile_id)
        if operation:
            conditions.append("operation = ?")
            values.append(operation)
        if state:
            conditions.append("state = ?")
            values.append(state)
        if hidden_patterns:
            conditions.append("activity_visible(data_json)")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        values.extend(
            (
                max(0, int(limit)),
                max(0, int(offset)),
            )
        )
        with self._connect(hidden_patterns) as connection:
            rows = connection.execute(
                f"""
                SELECT data_json
                FROM activities
                {where}
                ORDER BY timestamp DESC, event_id DESC
                LIMIT ?
                OFFSET ?
                """,
                values,
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                item = json.loads(row["data_json"])
            except (TypeError, ValueError):
                continue
            if isinstance(item, dict):
                result.append(item)
        return result

    def activity_count(
        self, profile_id: str | None = None,
        hidden_patterns: tuple[str, ...] = (),
    ) -> int:
        conditions = ["profile_id = ?"] if profile_id else []
        values = (profile_id,) if profile_id else ()
        if hidden_patterns:
            conditions.append("activity_visible(data_json)")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._connect(hidden_patterns) as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM activities
                {where}
                """,
                values,
            ).fetchone()
        return int(row["count"] or 0)

    def group_events(
        self,
        group_key: str,
        limit: int,
        profile_id: str | None = None,
        operation: str | None = None,
        state: str | None = None,
        hidden_patterns: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        conditions = ["group_key = ?"]
        values: list[Any] = [group_key]
        if profile_id:
            conditions.append("profile_id = ?")
            values.append(profile_id)
        if operation:
            conditions.append("operation = ?")
            values.append(operation)
        if state:
            conditions.append("state = ?")
            values.append(state)
        if hidden_patterns:
            conditions.append("activity_visible(data_json)")
        values.append(max(0, int(limit)))
        with self._connect(hidden_patterns) as connection:
            rows = connection.execute(
                f"""
                SELECT data_json
                FROM activities
                WHERE {' AND '.join(conditions)}
                ORDER BY timestamp DESC, event_id DESC
                LIMIT ?
                """,
                values,
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                item = json.loads(row["data_json"])
            except (TypeError, ValueError):
                continue
            if isinstance(item, dict):
                result.append(item)
        return result

    def recent_group_headers(
        self,
        limit: int,
        profile_id: str | None = None,
        operation: str | None = None,
        state: str | None = None,
        hidden_patterns: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        """Return the newest complete group summaries and one display item."""
        conditions = ["group_key != ''"]
        values: list[Any] = []
        if profile_id:
            conditions.append("profile_id = ?")
            values.append(profile_id)
        if operation:
            conditions.append("operation = ?")
            values.append(operation)
        if state:
            conditions.append("state = ?")
            values.append(state)
        if hidden_patterns:
            conditions.append("activity_visible(data_json)")
        values.append(max(0, int(limit)))
        representatives: dict[str, dict[str, Any]] = {}
        with self._connect(hidden_patterns) as connection:
            rows = connection.execute(
                f"""
                SELECT group_key
                FROM activities
                WHERE {' AND '.join(conditions)}
                GROUP BY group_key
                ORDER BY MAX(group_timestamp) DESC,
                         MAX(timestamp) DESC,
                         group_key DESC
                LIMIT ?
                """,
                values,
            ).fetchall()
            keys = [str(row["group_key"]) for row in rows]
            for key in keys:
                representative_conditions = ["group_key = ?"]
                representative_values: list[Any] = [key]
                if profile_id:
                    representative_conditions.append("profile_id = ?")
                    representative_values.append(profile_id)
                if operation:
                    representative_conditions.append("operation = ?")
                    representative_values.append(operation)
                if state:
                    representative_conditions.append("state = ?")
                    representative_values.append(state)
                if hidden_patterns:
                    representative_conditions.append("activity_visible(data_json)")
                row = connection.execute(
                    f"""
                    SELECT data_json
                    FROM activities
                    WHERE {' AND '.join(representative_conditions)}
                    ORDER BY timestamp DESC, event_id DESC
                    LIMIT 1
                    """,
                    representative_values,
                ).fetchone()
                if row is None:
                    continue
                try:
                    representative = json.loads(row["data_json"])
                except (TypeError, ValueError):
                    continue
                if isinstance(representative, dict):
                    representatives[key] = representative
        summaries = self.group_summaries(
            keys,
            profile_id=profile_id,
            operation=operation,
            state=state,
            hidden_patterns=hidden_patterns,
        )
        result: list[dict[str, Any]] = []
        for key in keys:
            summary = summaries.get(key)
            representative = representatives.get(key)
            if summary is None or representative is None:
                continue
            result.append(
                {
                    "group_key": key,
                    **summary,
                    "representative": representative,
                }
            )
        return result

    def group_summaries(
        self,
        group_keys: Iterable[str],
        profile_id: str | None = None,
        operation: str | None = None,
        state: str | None = None,
        hidden_patterns: tuple[str, ...] = (),
    ) -> dict[str, dict[str, Any]]:
        """Return complete totals for selected groups from persisted history."""
        unique_keys = list(dict.fromkeys(str(key) for key in group_keys if key))
        if not unique_keys:
            return {}

        result: dict[str, dict[str, Any]] = {}
        with self._connect(hidden_patterns) as connection:
            for start in range(0, len(unique_keys), 400):
                keys = unique_keys[start : start + 400]
                conditions = [
                    f"group_key IN ({','.join('?' for _key in keys)})"
                ]
                values: list[Any] = list(keys)
                if profile_id:
                    conditions.append("profile_id = ?")
                    values.append(profile_id)
                if operation:
                    conditions.append("operation = ?")
                    values.append(operation)
                if state:
                    conditions.append("state = ?")
                    values.append(state)
                if hidden_patterns:
                    conditions.append("activity_visible(data_json)")
                rows = connection.execute(
                    f"""
                    SELECT
                        group_key,
                        COUNT(*) AS item_count,
                        SUM(
                            CASE WHEN state IN ('uploading', 'retrying')
                                 THEN 1 ELSE 0 END
                        ) AS active_count,
                        SUM(
                            CASE WHEN state = 'queued' THEN 1 ELSE 0 END
                        ) AS queued_count,
                        SUM(
                            CASE WHEN state = 'completed' THEN 1 ELSE 0 END
                        ) AS completed_count,
                        SUM(
                            CASE WHEN state = 'error' THEN 1 ELSE 0 END
                        ) AS error_count,
                        SUM(size) AS total_size,
                        SUM(
                            CASE WHEN state = 'completed' THEN size ELSE 0 END
                        ) AS transferred_size,
                        MIN(operation) AS first_operation,
                        MAX(operation) AS last_operation,
                        MAX(group_timestamp) AS group_timestamp
                    FROM activities
                    WHERE {' AND '.join(conditions)}
                    GROUP BY group_key
                    """,
                    values,
                ).fetchall()
                for row in rows:
                    first_operation = str(row["first_operation"] or "")
                    last_operation = str(row["last_operation"] or "")
                    result[str(row["group_key"])] = {
                        "item_count": int(row["item_count"] or 0),
                        "active_count": int(row["active_count"] or 0),
                        "queued_count": int(row["queued_count"] or 0),
                        "completed_count": int(
                            row["completed_count"] or 0
                        ),
                        "error_count": int(row["error_count"] or 0),
                        "total_size": int(row["total_size"] or 0),
                        "transferred_size": int(
                            row["transferred_size"] or 0
                        ),
                        "operation": (
                            first_operation
                            if first_operation == last_operation
                            else "mixed"
                        ),
                        "timestamp": float(row["group_timestamp"] or 0),
                    }
        return result

    def clear_history(self) -> int:
        """Delete recorded activity without resetting live monitor state."""
        cleared_at = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM activities"
            ).fetchone()
            deleted = int(row["count"] or 0)
            connection.execute("DELETE FROM activities")
            connection.execute(
                """
                INSERT INTO store_metadata(key, value)
                VALUES ('history_cleared_at', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (repr(cleared_at),),
            )
        return deleted

    def read_new_log(
        self,
        path: Path,
        initial_tail_bytes: int,
        context_bytes: int = 8192,
    ) -> str:
        try:
            stat = path.stat()
        except OSError:
            return ""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT inode, offset FROM log_cursors WHERE path = ?",
                (str(path),),
            ).fetchone()

        chunks: list[tuple[bytes, int]] = []
        previous_inode = int(row["inode"]) if row is not None else 0
        committed_offset = int(row["offset"]) if row is not None else 0
        if row is not None and previous_inode != int(stat.st_ino):
            try:
                candidates = path.parent.iterdir()
                for candidate in candidates:
                    if candidate == path:
                        continue
                    try:
                        candidate_stat = candidate.stat()
                    except OSError:
                        continue
                    if int(candidate_stat.st_ino) != previous_inode:
                        continue
                    previous_offset = max(
                        0,
                        min(committed_offset, candidate_stat.st_size)
                        - max(0, context_bytes),
                        candidate_stat.st_size - initial_tail_bytes,
                    )
                    try:
                        with candidate.open("rb") as stream:
                            stream.seek(previous_offset)
                            chunks.append((stream.read(), previous_offset))
                    except OSError:
                        pass
                    break
            except OSError:
                pass

        if (
            row is not None
            and int(row["inode"]) == int(stat.st_ino)
            and int(row["offset"]) <= stat.st_size
        ):
            if committed_offset >= stat.st_size:
                return ""
            # Keep a short overlap so a result line such as
            # "Copied (replaced existing)" remains available when the
            # following VFS upload confirmation is written separately.
            offset = max(0, committed_offset - max(0, context_bytes))
        else:
            offset = max(0, stat.st_size - initial_tail_bytes)

        try:
            with path.open("rb") as stream:
                stream.seek(offset)
                chunks.append((stream.read(), offset))
                new_offset = stream.tell()
        except OSError:
            return ""

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO log_cursors(path, inode, offset)
                VALUES (?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    inode = excluded.inode,
                    offset = excluded.offset
                """,
                (str(path), int(stat.st_ino), int(new_offset)),
            )

        texts: list[str] = []
        for data, chunk_offset in chunks:
            text = data.decode("utf-8", errors="replace")
            if (
                chunk_offset > 0
                and text
                and not text.startswith(("20", "{"))
            ):
                _, separator, text = text.partition("\n")
                if not separator:
                    continue
            if text:
                texts.append(text)
        return "\n".join(texts)

    def observe_queue(
        self,
        profile_id: str,
        process_id: int,
        items: Iterable[dict[str, Any]],
        now: float,
    ) -> None:
        with self._connect() as connection:
            existing_rows = connection.execute(
                """
                SELECT queue_id, first_seen, path
                FROM queue_observations
                WHERE profile_id = ? AND process_id = ?
                """,
                (profile_id, process_id),
            ).fetchall()
            existing_by_queue_id = {
                int(row["queue_id"]): row for row in existing_rows
            }
            for item in items:
                try:
                    queue_id = int(item.get("queue_id", item.get("id", 0)))
                except (TypeError, ValueError):
                    continue
                path = str(item.get("path", item.get("name", ""))).strip("/")
                if not path:
                    continue
                existing = existing_by_queue_id.get(queue_id)
                first_seen = (
                    float(existing["first_seen"])
                    if (
                        existing is not None
                        and str(existing["path"]).strip("/") == path
                    )
                    else now
                )
                item["first_seen"] = first_seen
                item["group_timestamp"] = first_seen
                item["timestamp"] = first_seen
                payload = json.dumps(item, ensure_ascii=False, sort_keys=True)
                connection.execute(
                    """
                    INSERT INTO queue_observations (
                        profile_id, process_id, queue_id, path,
                        first_seen, last_seen, data_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(profile_id, process_id, queue_id) DO UPDATE SET
                        path = excluded.path,
                        last_seen = excluded.last_seen,
                        data_json = excluded.data_json
                    """,
                    (
                        profile_id,
                        process_id,
                        queue_id,
                        path,
                        now,
                        now,
                        payload,
                    ),
                )
            connection.execute(
                "DELETE FROM queue_observations WHERE last_seen < ?",
                (now - 7 * 86400,),
            )

    def observed_upload(
        self,
        profile_id: str,
        process_id: int,
        path: str,
        timestamp: float,
    ) -> dict[str, Any] | None:
        clean_path = path.strip("/")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT first_seen, data_json
                FROM queue_observations
                WHERE profile_id = ?
                  AND process_id = ?
                  AND last_seen >= ?
                  AND (
                      path = ?
                      OR substr(path, -(length(?) + 1)) = '/' || ?
                      OR substr(?, -(length(path) + 1)) = '/' || path
                  )
                ORDER BY last_seen DESC
                """,
                (
                    profile_id,
                    process_id,
                    timestamp - 3600,
                    clean_path,
                    clean_path,
                    clean_path,
                    clean_path,
                ),
            ).fetchall()
        for row in rows:
            try:
                item = json.loads(row["data_json"])
            except (TypeError, ValueError):
                continue
            first_seen = float(row["first_seen"])
            item.setdefault("first_seen", first_seen)
            item.setdefault("group_timestamp", first_seen)
            observed = str(item.get("path", item.get("name", ""))).strip("/")
            if (
                observed == clean_path
                or observed.endswith(f"/{clean_path}")
                or clean_path.endswith(f"/{observed}")
            ):
                return item
        return None

    def mark_transfers_seen(
        self,
        transfers: Iterable[tuple[str, float]],
    ) -> set[str]:
        inserted: set[str] = set()
        with self._connect() as connection:
            for transfer_id, timestamp in transfers:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO transferred_seen(
                        transfer_id, timestamp
                    ) VALUES (?, ?)
                    """,
                    (transfer_id, timestamp),
                )
                if cursor.rowcount:
                    inserted.add(transfer_id)
            connection.execute(
                "DELETE FROM transferred_seen WHERE timestamp < ?",
                (time.time() - 30 * 86400,),
            )
        return inserted

    def record_speed(self, timestamp: float, upload: float, download: float) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO speed_samples(timestamp, upload, download)
                VALUES (?, ?, ?)
                """,
                (timestamp, upload, download),
            )
            connection.execute(
                "DELETE FROM speed_samples WHERE timestamp < ?",
                (timestamp - 86400,),
            )

    def speed_history(self, seconds: int = 300) -> list[dict[str, float]]:
        since = time.time() - max(1, seconds)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT timestamp, upload, download
                FROM speed_samples
                WHERE timestamp >= ?
                ORDER BY timestamp
                """,
                (since,),
            ).fetchall()
        return [
            {
                "timestamp": float(row["timestamp"]),
                "upload": float(row["upload"]),
                "download": float(row["download"]),
            }
            for row in rows
        ]

    def summary(
        self,
        profile_id: str | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        periods = {
            "day": now - 86400,
            "month": now - 30 * 86400,
        }
        result: dict[str, Any] = {}
        with self._connect() as connection:
            for name, since in periods.items():
                where = "WHERE timestamp >= ?"
                values: list[Any] = [since]
                if profile_id:
                    where += " AND profile_id = ?"
                    values.append(profile_id)
                row = connection.execute(
                    f"""
                    SELECT
                        SUM(
                            CASE WHEN state = 'completed'
                                      AND operation IN ('copy', 'modify')
                                 THEN size ELSE 0 END
                        ) AS bytes,
                        SUM(
                            CASE WHEN state = 'completed' THEN 1 ELSE 0 END
                        ) AS completed,
                        SUM(
                            CASE WHEN state = 'error' THEN 1 ELSE 0 END
                        ) AS errors,
                        SUM(
                            CASE WHEN operation = 'delete' THEN 1 ELSE 0 END
                        ) AS deleted,
                        SUM(
                            CASE WHEN operation = 'rename' THEN 1 ELSE 0 END
                        ) AS renamed,
                        SUM(
                            CASE WHEN operation = 'modify' THEN 1 ELSE 0 END
                        ) AS modified
                    FROM activities
                    {where}
                    """,
                    values,
                ).fetchone()
                result[name] = {
                    "bytes": int(row["bytes"] or 0),
                    "completed": int(row["completed"] or 0),
                    "errors": int(row["errors"] or 0),
                    "deleted": int(row["deleted"] or 0),
                    "renamed": int(row["renamed"] or 0),
                    "modified": int(row["modified"] or 0),
                }
        return result
