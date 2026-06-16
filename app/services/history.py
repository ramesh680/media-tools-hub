from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any
import json
import sqlite3

from app.models import utc_now_iso


class HistoryRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._lock = RLock()

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tracker_history (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tracker_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tracker_history_created_at "
                "ON tracker_history(created_at DESC)"
            )

    def add_run(self, snapshot: dict[str, Any]) -> int:
        row_count = sum(section.get("row_count", 0) for section in snapshot.get("sections", []))
        created_at = snapshot.get("created_at") or utc_now_iso()
        snapshot["created_at"] = created_at
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO tracker_history
                (tracker_type, title, row_count, created_at, snapshot_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    snapshot["tracker_type"],
                    snapshot["title"],
                    row_count,
                    created_at,
                    json.dumps(snapshot, ensure_ascii=True),
                ),
            )
            return int(cursor.lastrowid)

    def list_recent(self, limit: int = 12) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT run_id, tracker_type, title, row_count, created_at
                FROM tracker_history
                ORDER BY datetime(created_at) DESC, run_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT run_id, tracker_type, title, row_count, created_at, snapshot_json
                FROM tracker_history
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        if not row:
            return None
        payload = dict(row)
        snapshot = json.loads(payload["snapshot_json"])
        snapshot["run_id"] = payload["run_id"]
        return snapshot

    def list_snapshots(
        self,
        tracker_type: str,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT run_id, created_at, snapshot_json
                FROM tracker_history
                WHERE tracker_type = ?
                ORDER BY run_id DESC
                LIMIT ?
                """,
                (tracker_type, limit),
            ).fetchall()

        snapshots: list[dict[str, Any]] = []
        since_utc = _as_utc_datetime(since) if since else None
        for row in rows:
            created_at = _parse_iso_datetime(row["created_at"])
            if since_utc and created_at and created_at < since_utc:
                continue
            snapshot = json.loads(row["snapshot_json"])
            snapshot["run_id"] = row["run_id"]
            snapshots.append(snapshot)
        return snapshots

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection


def _parse_iso_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return _as_utc_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _as_utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
