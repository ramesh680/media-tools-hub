from __future__ import annotations

from pathlib import Path
from threading import RLock
from typing import Any
import json
import sqlite3

from app.models import utc_now_iso


class ValidatorHistoryRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._lock = RLock()

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS validator_history (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    run_by TEXT NOT NULL,
                    ip_address TEXT NOT NULL,
                    source_file TEXT NOT NULL,
                    validated_file TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    error_count INTEGER NOT NULL,
                    suggestion_count INTEGER NOT NULL,
                    report_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_validator_history_created_at "
                "ON validator_history(created_at DESC)"
            )

    def add_run(
        self,
        result: dict[str, Any],
        run_by: str,
        ip_address: str,
        source_file: str,
        validated_file: str,
    ) -> int:
        created_at = result.get("created_at") or utc_now_iso()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO validator_history
                (created_at, run_by, ip_address, source_file, validated_file, row_count,
                 error_count, suggestion_count, report_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    run_by or "-",
                    ip_address or "-",
                    source_file,
                    validated_file,
                    int(result.get("row_count", 0)),
                    int(result.get("error_count", 0)),
                    int(result.get("suggestion_count", 0)),
                    json.dumps(result, ensure_ascii=True),
                ),
            )
            return int(cursor.lastrowid)

    def list_recent(self, limit: int = 12) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT run_id, created_at, run_by, ip_address, source_file, validated_file,
                       row_count, error_count, suggestion_count
                FROM validator_history
                ORDER BY datetime(created_at) DESC, run_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection
