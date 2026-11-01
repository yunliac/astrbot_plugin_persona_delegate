"""Versioned, per-database startup upgrades; never guess private alias ownership.

Run before a store opens its live connection. Legacy version 0 is the supported
unversioned SQLite format. Backups include committed WAL content. Each database
is independently transactional/resumable; no cross-database crash atomicity is
claimed. New databases need no backup. Unknown/newer formats fail closed.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import tempfile

DATA_VERSION = 2
_EXPECTED = {
    "tasks": {
        "tasks": "id status mode target target_name goal owner_key owner_name origin report_to turns created updated deadline transcript persona_id persona_prompt provider_id",
        "outbox": "id task_id report_to text attempts next_attempt created",
        "followups": "target task_id until count",
    },
    "roster": {
        "groups": "platform group_id name member_count synced",
        "members": "platform group_id qq nickname card source updated",
        "aliases": "platform scope_group owner_key alias qq",
    },
    "blocklist": {"blocked": "platform account added_by added_at"},
    "optout": {
        "optouts": "platform account until created",
        "sender_blocks": "platform account sender_account created",
    },
}


def upgrade_database(db_path, store):
    """Upgrade known legacy columns atomically, retaining every existing row.

    Missing tables are created by the existing store initializer afterwards.
    Return version/change/backup details. Unknown missing fields are rejected
    before making any changes. Version is not the plugin release version.
    """
    if store not in _EXPECTED:
        raise ValueError("unknown database store")
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > DATA_VERSION or version < 0:
            raise RuntimeError(f"{store}: unsupported data version {version}; refusing downgrade")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError(f"{store}: database integrity check failed")
        existing = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        changes = []
        for table, expected in _EXPECTED[store].items():
            if table not in existing:
                continue
            columns = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
            missing = set(expected.split()) - columns
            if version == 0 and (store, table) == ("tasks", "tasks") and "owner_name" in missing:
                changes.append("ALTER TABLE tasks ADD COLUMN owner_name TEXT NOT NULL DEFAULT ''")
                missing.remove("owner_name")
            if version == 0 and (store, table) == ("roster", "members") and "source" in missing:
                # Old records without provenance cannot grant group permissions.
                changes.append("ALTER TABLE members ADD COLUMN source TEXT NOT NULL DEFAULT 'mention'")
                missing.remove("source")
            if missing:
                raise RuntimeError(f"{store}.{table}: unsupported legacy fields {sorted(missing)}; manual migration required (no ownership guessing)")
        if version == DATA_VERSION:
            return {"store": store, "version": version, "upgraded": False, "backup_path": None}
        if store == "optout" and "sender_blocks" not in existing:
            changes.append(
                "CREATE TABLE sender_blocks (platform TEXT NOT NULL, account TEXT NOT NULL, "
                "sender_account TEXT NOT NULL, created REAL NOT NULL, "
                "PRIMARY KEY(platform, account, sender_account))"
            )
        backup_path = None
        if existing:
            root = path.parent / "schema-backups"
            root.mkdir(mode=0o700, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ-")
            folder = Path(tempfile.mkdtemp(prefix=f"{store}-v{version}-" + stamp, dir=root))
            os.chmod(folder, 0o700)
            destination = folder / path.name
            fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            backup = sqlite3.connect(str(destination))
            try:
                db.backup(backup)
                if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("schema backup integrity check failed")
            finally:
                backup.close()
            backup_path = str(destination)
        try:
            db.execute("BEGIN IMMEDIATE")
            for sql in changes:
                db.execute(sql)
            db.execute(f"PRAGMA user_version={DATA_VERSION}")
            db.commit()
        except BaseException:
            db.rollback()
            raise
        return {"store": store, "version": DATA_VERSION, "upgraded": True,
                "backup_path": backup_path, "changes": len(changes)}
    finally:
        db.close()
