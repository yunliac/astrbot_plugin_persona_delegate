"""Startup upgrade tests use real legacy SQLite files, without AstrBot."""
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("schema_upgrade_test", Path(__file__).resolve().parents[1] / "schema_upgrade.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class SchemaUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "store.sqlite3"

    def connect(self):
        db = sqlite3.connect(self.path)
        self.addCleanup(db.close)
        return db

    def test_new_database_and_repeat_are_idempotent(self):
        self.assertTrue(m.upgrade_database(self.path, "roster")["upgraded"])
        self.assertFalse(m.upgrade_database(self.path, "roster")["upgraded"])
        self.assertFalse((self.path.parent / "schema-backups").exists())

    def test_legacy_members_gain_safe_source_and_backup_preserves_wal(self):
        db = self.connect()
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE members(platform TEXT,group_id TEXT,qq TEXT,nickname TEXT,card TEXT,updated REAL)")
        db.execute("INSERT INTO members VALUES('qq','g','123456','小明','',123)")
        db.commit()
        result = m.upgrade_database(self.path, "roster")
        self.assertEqual(db.execute("SELECT nickname,source FROM members").fetchone(), ("小明", "mention"))
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
        backup = sqlite3.connect(result["backup_path"])
        try:
            self.assertEqual(backup.execute("SELECT nickname FROM members").fetchone()[0], "小明")
            self.assertNotIn("source", [r[1] for r in backup.execute("PRAGMA table_info(members)")])
        finally:
            backup.close()
        self.assertFalse(m.upgrade_database(self.path, "roster")["upgraded"])

    def test_ownerless_aliases_rejected_without_guessing_or_modifying(self):
        db = self.connect()
        db.execute("CREATE TABLE aliases(platform TEXT,alias TEXT,qq TEXT)")
        db.execute("INSERT INTO aliases VALUES('qq','阿乖','123456')")
        db.commit()
        with self.assertRaisesRegex(RuntimeError, "ownership guessing"):
            m.upgrade_database(self.path, "roster")
        self.assertEqual(db.execute("SELECT * FROM aliases").fetchall(), [("qq", "阿乖", "123456")])
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)

    def test_current_private_alias_ownership_preserved(self):
        db = self.connect()
        db.execute("CREATE TABLE aliases(platform TEXT,scope_group TEXT,owner_key TEXT,alias TEXT,qq TEXT)")
        rows = [('qq', 'g1', 'qq:one', '阿乖', '123456'), ('qq', '', 'qq:two', '阿乖', '234567')]
        db.executemany("INSERT INTO aliases VALUES(?,?,?,?,?)", rows)
        db.commit()
        result = m.upgrade_database(self.path, "roster")
        self.assertTrue(Path(result["backup_path"]).is_file())
        self.assertEqual(db.execute("SELECT * FROM aliases").fetchall(), rows)

    def test_newer_version_refuses_downgrade(self):
        db = self.connect()
        db.execute("PRAGMA user_version=99")
        with self.assertRaisesRegex(RuntimeError, "refusing downgrade"):
            m.upgrade_database(self.path, "tasks")
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 99)

    def test_legacy_task_adds_owner_name_without_losing_rows(self):
        db = self.connect()
        cols = m._EXPECTED['tasks']['tasks'].split()
        cols.remove('owner_name')
        db.execute('CREATE TABLE tasks (' + ','.join('"' + c + '" TEXT' for c in cols) + ')')
        db.execute('INSERT INTO tasks(id,goal) VALUES(?,?)', ('old-id','保留原委托'))
        db.commit()
        m.upgrade_database(self.path, 'tasks')
        self.assertEqual(db.execute('SELECT id,goal,owner_name FROM tasks').fetchone(), ('old-id','保留原委托',''))

    def test_v1_all_stores_upgrade_with_versioned_backups(self):
        for store in ("tasks", "roster", "blocklist", "optout"):
            with self.subTest(store=store):
                path = self.path.parent / (store + ".sqlite3")
                db = sqlite3.connect(path)
                self.addCleanup(db.close)
                # All stores must be backed up even when no ALTER is needed.
                db.execute("CREATE TABLE sentinel(value TEXT)")
                db.execute("INSERT INTO sentinel VALUES('preserved')")
                if store == "optout":
                    db.execute("CREATE TABLE optouts(platform TEXT,account TEXT,until REAL,created REAL,PRIMARY KEY(platform,account))")
                    db.execute("INSERT INTO optouts VALUES('qq','receiver',0,1)")
                db.execute("PRAGMA user_version=1")
                db.commit()
                result = m.upgrade_database(path, store)
                self.assertEqual(result["version"], 2)
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertEqual(db.execute("SELECT value FROM sentinel").fetchone()[0], "preserved")
                backup = sqlite3.connect(result["backup_path"])
                try:
                    self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 1)
                    self.assertEqual(backup.execute("SELECT value FROM sentinel").fetchone()[0], "preserved")
                    if store == "optout":
                        self.assertEqual(db.execute("SELECT * FROM optouts").fetchone(), ("qq", "receiver", 0, 1))
                        self.assertEqual(db.execute("SELECT * FROM sender_blocks").fetchall(), [])
                        self.assertEqual(backup.execute("SELECT name FROM sqlite_master WHERE name='sender_blocks'").fetchall(), [])
                finally:
                    backup.close()
                self.assertFalse(m.upgrade_database(path, store)["upgraded"])

    def test_new_optout_database_gets_v2_sender_blocks_without_backup(self):
        result = m.upgrade_database(self.path, "optout")
        self.assertEqual(result["version"], 2)
        self.assertIsNone(result["backup_path"])
        db = self.connect()
        columns = db.execute("PRAGMA table_info(sender_blocks)").fetchall()
        self.assertEqual([r[1] for r in columns], ["platform", "account", "sender_account", "created"])
        self.assertEqual([r[5] for r in columns], [1, 2, 3, 0])

    def test_existing_sender_blocks_are_preserved_during_upgrade(self):
        db = self.connect()
        db.execute("CREATE TABLE sender_blocks(platform TEXT,account TEXT,sender_account TEXT,created REAL,PRIMARY KEY(platform,account,sender_account))")
        db.execute("INSERT INTO sender_blocks VALUES('qq','receiver','sender',3)")
        db.commit()
        m.upgrade_database(self.path, "optout")
        self.assertEqual(db.execute("SELECT * FROM sender_blocks").fetchall(), [("qq", "receiver", "sender", 3)])

    def test_invalid_sender_blocks_schema_rejected_before_backup(self):
        db = self.connect()
        db.execute("CREATE TABLE sender_blocks(platform TEXT,account TEXT,created REAL)")
        db.execute("PRAGMA user_version=1")
        with self.assertRaisesRegex(RuntimeError, "sender_account"):
            m.upgrade_database(self.path, "optout")
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertFalse((self.path.parent / "schema-backups").exists())

    def test_corrupt_database_does_not_get_overwritten(self):
        self.path.write_bytes(b'not sqlite')
        with self.assertRaises(sqlite3.DatabaseError):
            m.upgrade_database(self.path, 'tasks')
        self.assertEqual(self.path.read_bytes(), b'not sqlite')
