"""群成员名册：每日快照 + @ 即时收录（标准库 SQLite）。

表结构：
- groups(platform, group_id, name, member_count, synced)
- members(platform, group_id, qq, nickname, card, source, updated)

``source`` 为 ``sync``（每日成员列表快照）或 ``mention``（群内 @ 时即时收录）。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any


class Roster:
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
            CREATE TABLE IF NOT EXISTS groups (
                platform TEXT NOT NULL, group_id TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '', member_count INTEGER NOT NULL DEFAULT 0,
                synced REAL NOT NULL,
                PRIMARY KEY(platform, group_id)
            );
            CREATE TABLE IF NOT EXISTS members (
                platform TEXT NOT NULL, group_id TEXT NOT NULL, qq TEXT NOT NULL,
                nickname TEXT NOT NULL DEFAULT '', card TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'sync', updated REAL NOT NULL,
                PRIMARY KEY(platform, group_id, qq)
            );
            CREATE INDEX IF NOT EXISTS members_lookup ON members(platform, group_id, nickname);
            CREATE INDEX IF NOT EXISTS members_qq ON members(platform, qq);
        """)
        self._db = db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("Roster.initialize() must be called first")
        return self._db

    def replace_group(self, platform: str, group_id: str, name: str, members: list[dict[str, Any]]) -> int:
        """用最新成员快照替换该群名册（不在列表中的成员会被移除）。"""
        now = time.time()
        rows = []
        seen = set()
        for member in members:
            qq = str(member.get("qq") or "").strip()
            if not qq or qq in seen:
                continue
            seen.add(qq)
            rows.append((
                str(platform), str(group_id), qq,
                str(member.get("nickname") or "").strip(),
                str(member.get("card") or "").strip(),
                now,
            ))
        with self._conn():
            self._conn().execute(
                "INSERT INTO groups(platform,group_id,name,member_count,synced) VALUES(?,?,?,?,?) "
                "ON CONFLICT(platform,group_id) DO UPDATE SET name=excluded.name, "
                "member_count=excluded.member_count, synced=excluded.synced",
                (str(platform), str(group_id), str(name or "").strip(), len(rows), now),
            )
            self._conn().execute(
                "DELETE FROM members WHERE platform=? AND group_id=?",
                (str(platform), str(group_id)),
            )
            self._conn().executemany(
                "INSERT INTO members(platform,group_id,qq,nickname,card,source,updated) "
                "VALUES(?,?,?,?,?,'sync',?)",
                rows,
            )
        return len(rows)

    def prune_groups(self, platform: str, keep_ids: list[str]) -> None:
        """删除本次同步未出现的群（机器人已退群）。"""
        keep = {str(item) for item in keep_ids}
        rows = self._conn().execute(
            "SELECT group_id FROM groups WHERE platform=?", (str(platform),),
        ).fetchall()
        stale = [row["group_id"] for row in rows if row["group_id"] not in keep]
        if not stale:
            return
        with self._conn():
            for group_id in stale:
                self._conn().execute(
                    "DELETE FROM members WHERE platform=? AND group_id=?",
                    (str(platform), group_id),
                )
                self._conn().execute(
                    "DELETE FROM groups WHERE platform=? AND group_id=?",
                    (str(platform), group_id),
                )

    def remember_mention(self, platform: str, group_id: str, qq: str, nickname: str) -> None:
        """群内 @ 时即时收录；已有同步数据只补空昵称。"""
        qq = str(qq or "").strip()
        if not qq:
            return
        now = time.time()
        with self._conn():
            self._conn().execute(
                "INSERT INTO groups(platform,group_id,name,member_count,synced) VALUES(?,?,'',0,?) "
                "ON CONFLICT(platform,group_id) DO NOTHING",
                (str(platform), str(group_id), now),
            )
            self._conn().execute(
                "INSERT INTO members(platform,group_id,qq,nickname,card,source,updated) "
                "VALUES(?,?,?,?,'','mention',?) "
                "ON CONFLICT(platform,group_id,qq) DO UPDATE SET "
                "nickname=CASE WHEN members.nickname='' THEN excluded.nickname ELSE members.nickname END, "
                "updated=excluded.updated",
                (str(platform), str(group_id), qq, str(nickname or "").strip(), now),
            )

    def find_member(self, platform: str, group_id: str, name: str) -> dict[str, Any] | None:
        text = str(name or "").strip().lstrip("@")
        if not text:
            return None
        row = self._conn().execute(
            "SELECT * FROM members WHERE platform=? AND group_id=? "
            "AND (nickname=? OR card=? OR qq=?) ORDER BY updated DESC LIMIT 1",
            (str(platform), str(group_id), text, text, text),
        ).fetchone()
        return dict(row) if row else None

    def find_member_any(self, platform: str, name: str) -> dict[str, Any] | None:
        """跨群按昵称查找；只有唯一 QQ 时才返回，避免同名误发。"""
        text = str(name or "").strip().lstrip("@")
        if not text:
            return None
        rows = self._conn().execute(
            "SELECT * FROM members WHERE platform=? AND (nickname=? OR card=? OR qq=?) "
            "ORDER BY updated DESC",
            (str(platform), text, text, text),
        ).fetchall()
        if not rows:
            return None
        if len({row["qq"] for row in rows}) != 1:
            return None
        return dict(rows[0])

    def stats(self) -> dict[str, Any]:
        groups = self._conn().execute("SELECT COUNT(*) AS n, MAX(synced) AS last FROM groups").fetchone()
        members = self._conn().execute("SELECT COUNT(*) AS n FROM members").fetchone()
        return {"groups": groups["n"], "members": members["n"], "last_sync": groups["last"]}

    def group_name(self, platform: str, group_id: str) -> str:
        row = self._conn().execute(
            "SELECT name FROM groups WHERE platform=? AND group_id=?",
            (str(platform), str(group_id)),
        ).fetchone()
        return str(row["name"]) if row else ""

    def group_for(self, platform: str, qq: str) -> str:
        """返回该 QQ 最近出现过的群号；用于没有显式群提示时的临时会话兜底。"""
        row = self._conn().execute(
            "SELECT group_id FROM members WHERE platform=? AND qq=? ORDER BY updated DESC LIMIT 1",
            (str(platform), str(qq)),
        ).fetchone()
        return str(row["group_id"]) if row else ""
