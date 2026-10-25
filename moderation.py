"""运行期黑名单（标准库 SQLite）。

管理员可在聊天里拉黑/解除拉黑；配置里的 ``blocked_users`` 作为静态黑名单，
两者都生效。``platform`` 为 ``*`` 时表示该账号在所有平台都被拒绝。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any


class Blocklist:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._db: sqlite3.Connection | None = None

    def initialize(self) -> None:
        if self._db is not None:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.db_path))
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS blocked (
                platform TEXT NOT NULL, account TEXT NOT NULL,
                added_by TEXT NOT NULL DEFAULT '', added_at REAL NOT NULL,
                PRIMARY KEY(platform, account)
            );
        """)
        self._db = db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("Blocklist.initialize() must be called first")
        return self._db

    def add(self, platform: str, account: str, added_by: str = "") -> None:
        platform = str(platform or "*").strip() or "*"
        account = str(account or "").strip()
        if not account:
            raise ValueError("账号不能为空")
        with self._conn():
            self._conn().execute(
                "INSERT INTO blocked(platform,account,added_by,added_at) VALUES(?,?,?,?) "
                "ON CONFLICT(platform,account) DO UPDATE SET added_by=excluded.added_by, added_at=excluded.added_at",
                (platform, account, str(added_by or ""), time.time()),
            )

    def remove(self, platform: str, account: str) -> bool:
        platform = str(platform or "*").strip() or "*"
        account = str(account or "").strip()
        cur = self._conn().execute(
            "DELETE FROM blocked WHERE platform=? AND account=?",
            (platform, account),
        )
        self._conn().commit()
        return cur.rowcount > 0

    def is_blocked(self, platform: str, account: str) -> bool:
        account = str(account or "").strip()
        if not account:
            return False
        row = self._conn().execute(
            "SELECT 1 FROM blocked WHERE account=? AND platform IN (?, '*') LIMIT 1",
            (account, str(platform or "")),
        ).fetchone()
        return row is not None

    def entries(self) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT platform, account, added_by, added_at FROM blocked ORDER BY added_at DESC",
        ).fetchall()
        return [dict(row) for row in rows]
