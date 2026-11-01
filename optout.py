"""私聊退订（/td）状态存储（标准库 SQLite）。

``until = 0`` 表示永久退订；否则为到期时间戳。退订只影响机器人主动私聊，
不影响群内 @。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any


class OptOut:
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
            CREATE TABLE IF NOT EXISTS optouts (
                platform TEXT NOT NULL, account TEXT NOT NULL,
                until REAL NOT NULL DEFAULT 0, created REAL NOT NULL,
                PRIMARY KEY(platform, account)
            );
            CREATE TABLE IF NOT EXISTS sender_blocks (
                platform TEXT NOT NULL, account TEXT NOT NULL,
                sender_account TEXT NOT NULL, created REAL NOT NULL,
                PRIMARY KEY(platform, account, sender_account)
            );
        """)
        self._db = db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("OptOut.initialize() must be called first")
        return self._db

    def set(self, platform: str, account: str, seconds: float) -> float:
        """seconds=0 表示永久；返回到期时间戳（0 为永久）。"""
        account = str(account or "").strip()
        if not account:
            raise ValueError("账号不能为空")
        until = 0.0 if seconds == 0 else time.time() + float(seconds)
        with self._conn():
            self._conn().execute(
                "INSERT INTO optouts(platform,account,until,created) VALUES(?,?,?,?) "
                "ON CONFLICT(platform,account) DO UPDATE SET until=excluded.until, created=excluded.created",
                (str(platform or ""), account, until, time.time()),
            )
        return until

    def clear(self, platform: str, account: str) -> bool:
        cur = self._conn().execute(
            "DELETE FROM optouts WHERE platform=? AND account=?",
            (str(platform or ""), str(account or "").strip()),
        )
        self._conn().commit()
        return cur.rowcount > 0

    def is_opted_out(self, platform: str, account: str) -> bool:
        account = str(account or "").strip()
        if not account:
            return False
        row = self._conn().execute(
            "SELECT until FROM optouts WHERE platform=? AND account=?",
            (str(platform or ""), account),
        ).fetchone()
        if row is None:
            return False
        until = float(row["until"])
        return until == 0 or until > time.time()

    def status(self, platform: str, account: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT platform, account, until, created FROM optouts WHERE platform=? AND account=?",
            (str(platform or ""), str(account or "").strip()),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        if data["until"] != 0 and data["until"] <= time.time():
            return None
        return data

    def block_sender(self, platform: str, account: str, sender_account: str) -> None:
        """永久拒收指定委托人；account 为接收者，重复添加保留创建时间。"""
        account = str(account or "").strip()
        sender_account = str(sender_account or "").strip()
        if not account or not sender_account:
            raise ValueError("接收者和委托人账号不能为空")
        with self._conn():
            self._conn().execute(
                "INSERT OR IGNORE INTO sender_blocks(platform,account,sender_account,created) VALUES(?,?,?,?)",
                (str(platform or ""), account, sender_account, time.time()),
            )

    def unblock_sender(self, platform: str, account: str, sender_account: str) -> bool:
        """移除单条按人拒收记录，返回是否实际删除。"""
        with self._conn():
            cur = self._conn().execute(
                "DELETE FROM sender_blocks WHERE platform=? AND account=? AND sender_account=?",
                (str(platform or ""), str(account or "").strip(), str(sender_account or "").strip()),
            )
        return cur.rowcount > 0

    def is_sender_blocked(self, platform: str, account: str, sender_account: str) -> bool:
        """仅查询指定平台、接收者和委托人的组合，不受全局退订影响。"""
        row = self._conn().execute(
            "SELECT 1 FROM sender_blocks WHERE platform=? AND account=? AND sender_account=?",
            (str(platform or ""), str(account or "").strip(), str(sender_account or "").strip()),
        ).fetchone()
        return row is not None

    def blocked_senders(self, platform: str, account: str) -> list[dict[str, Any]]:
        """返回接收者的按人拒收记录（最近添加优先，同时间按账号排序）。"""
        rows = self._conn().execute(
            "SELECT platform,account,sender_account,created FROM sender_blocks "
            "WHERE platform=? AND account=? ORDER BY created DESC,sender_account",
            (str(platform or ""), str(account or "").strip()),
        ).fetchall()
        return [dict(row) for row in rows]

    def prune_expired(self) -> int:
        cur = self._conn().execute(
            "DELETE FROM optouts WHERE until>0 AND until<=?", (time.time(),),
        )
        self._conn().commit()
        return cur.rowcount

    def entries(self) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT platform, account, until, created FROM optouts ORDER BY created DESC",
        ).fetchall()
        return [dict(row) for row in rows]
