"""Migration tests using actual CREATE TABLE literals, without AstrBot imports."""
import ast
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("delegate_migration_under_test", ROOT / "migration.py")
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.connections = []
        self.source = self.stores("source")
        self.target = self.stores("target")
        self.bundle = self.root / "bundle.json"
        self.seed(self.source)

    def tearDown(self):
        for db in self.connections:
            db.close()
        self.temp.cleanup()

    def stores(self, name):
        directory = self.root / name
        directory.mkdir()
        result = {}
        for store, module in (("tasks", "engine"), ("roster", "roster"),
                              ("blocklist", "moderation"), ("optout", "optout")):
            db = sqlite3.connect(str(directory / (store + ".sqlite3")))
            self.connections.append(db)
            db.execute("PRAGMA journal_mode=WAL")
            tree = ast.parse((ROOT / (module + ".py")).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "executescript" and node.args
                        and isinstance(node.args[0], ast.Constant)):
                    db.executescript(node.args[0].value)
            result[store] = db
        return result

    def insert(self, stores, store, table, row):
        with stores[store]:
            stores[store].execute("INSERT INTO " + table + " (" + ",".join(row) + ") VALUES ("
                                  + ",".join("?" for _ in row) + ")", tuple(row.values()))

    def task(self, task_id="task-1", status="waiting"):
        return dict(id=task_id, status=status, mode="single", target="old:FriendMessage:222",
                    target_name="old:name", goal="goal", owner_key="old:111", owner_name="owner",
                    origin="old:GroupMessage:333", report_to="old:FriendMessage:111", turns=0,
                    created=1.0, updated=2.0, deadline=30.0,
                    transcript='[{"role":"user","text":"hello","at":2.0}]',
                    persona_id="old:persona", persona_prompt="old:prompt", provider_id="old:provider")

    def seed(self, stores):
        self.insert(stores, "tasks", "tasks", self.task())
        self.insert(stores, "tasks", "outbox", dict(id=1, task_id="task-1", report_to="old:111",
                    text="do not replay", attempts=0, next_attempt=2.0, created=1.0))
        self.insert(stores, "tasks", "followups", dict(target="old:FriendMessage:222", task_id="task-1", until=30.0, count=0))
        self.insert(stores, "roster", "groups", dict(platform="old", group_id="333", name="group", member_count=1, synced=2.0))
        self.insert(stores, "roster", "members", dict(platform="old", group_id="333", qq="222", nickname="friend", card="", source="sync", updated=2.0))
        self.insert(stores, "roster", "aliases", dict(platform="old", scope_group="333", owner_key="old:111", alias="friend", qq="222"))
        self.insert(stores, "blocklist", "blocked", dict(platform="old", account="444", added_by="old:111", added_at=2.0))
        self.insert(stores, "optout", "optouts", dict(platform="old", account="555", until=0.0, created=2.0))
        self.insert(stores, "optout", "sender_blocks", dict(platform="old", account="555", sender_account="111", created=3.0))

    def export(self):
        return migration.export_bundle(self.source, self.bundle)

    def data(self):
        return json.loads(self.bundle.read_text(encoding="utf-8"))

    def save(self, data):
        self.bundle.write_text(json.dumps(data), encoding="utf-8")

    def snapshot(self, stores):
        return {table: list(stores[store].execute("SELECT * FROM " + table))
                for store, names in migration._TABLES.items() for table in names}

    def assert_rejected(self, change):
        self.export()
        data = self.data()
        change(data)
        self.save(data)
        before = self.snapshot(self.target)
        with self.assertRaises(ValueError):
            migration.import_bundle(self.target, self.bundle)
        self.assertEqual(before, self.snapshot(self.target))
        self.assertFalse((self.root / "target" / "migration-backups").exists())

    def test_roundtrip_no_replay_backups_and_idempotence(self):
        exported = self.export()
        self.assertEqual(set(exported["counts"]), set(migration._stores(self.source)[0]))
        self.assertEqual(exported["counts"]["outbox"], 1)
        report = migration.import_bundle(self.target, self.bundle)
        self.assertEqual((report["inserted"], report["skipped"]), (7, 2))
        self.assertEqual(self.target["tasks"].execute("SELECT id,status FROM tasks").fetchone(), ("task-1", "interrupted"))
        self.assertEqual(self.target["roster"].execute("SELECT source FROM members").fetchone()[0], "mention")
        for table in ("outbox", "followups"):
            self.assertEqual(self.target["tasks"].execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)
        backup = Path(report["backup_path"])
        self.assertEqual(len(list(backup.iterdir())), 4)
        db = sqlite3.connect(str(backup / "tasks.sqlite3"))
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        finally:
            db.close()
        again = migration.import_bundle(self.target, self.bundle)
        self.assertEqual((again["inserted"], again["skipped"]), (0, 9))

    def test_map_exact_prefix_only(self):
        self.export()
        report = migration.import_bundle(self.target, self.bundle, {"old": "new"})
        row = dict(zip([r[1] for r in self.target["tasks"].execute("PRAGMA table_info(tasks)")],
                       self.target["tasks"].execute("SELECT * FROM tasks").fetchone()))
        self.assertEqual(row["owner_key"], "new:111")
        self.assertEqual(row["origin"], "new:GroupMessage:333")
        self.assertEqual(row["target"], "new:FriendMessage:222")
        self.assertEqual(row["report_to"], "new:FriendMessage:111")
        self.assertEqual(row["persona_id"], "old:persona")
        self.assertEqual(row["provider_id"], "old:provider")
        self.assertEqual(row["target_name"], "old:name")
        self.assertEqual(report["platforms"], ["new"])
        self.assertEqual(self.target["blocklist"].execute("SELECT platform,added_by FROM blocked").fetchone(), ("new", "new:111"))
        self.assertEqual(self.target["roster"].execute("SELECT owner_key FROM aliases").fetchone()[0], "new:111")

    def test_existing_rows_sync_and_active_target_preserved(self):
        self.seed(self.target)
        self.insert(self.source, "tasks", "tasks", self.task("task-2", "completed"))
        self.export()
        before_outbox = list(self.target["tasks"].execute("SELECT * FROM outbox"))
        migration.import_bundle(self.target, self.bundle)
        self.assertEqual(self.target["tasks"].execute("SELECT status FROM tasks WHERE id='task-1'").fetchone()[0], "waiting")
        self.assertEqual(self.target["roster"].execute("SELECT source FROM members").fetchone()[0], "sync")
        self.assertEqual(before_outbox, list(self.target["tasks"].execute("SELECT * FROM outbox")))

    def test_new_active_id_same_active_target_imported_interrupted(self):
        self.insert(self.target, "tasks", "tasks", self.task("local", "waiting"))
        self.export()
        migration.import_bundle(self.target, self.bundle)
        self.assertEqual(self.target["tasks"].execute("SELECT status FROM tasks WHERE id='task-1'").fetchone()[0], "interrupted")
        self.assertEqual(self.target["tasks"].execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 2)

    def test_dry_run_no_writes_and_accurate_duplicate_counts(self):
        self.export()
        before = self.snapshot(self.target)
        report = migration.import_bundle(self.target, self.bundle, dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertIsNone(report["backup_path"])
        self.assertEqual(before, self.snapshot(self.target))
        self.assertFalse((self.root / "target" / "migration-backups").exists())
        actual = migration.import_bundle(self.target, self.bundle)
        self.assertEqual(report["counts"], actual["counts"])

    def test_v2_export_includes_sender_blocks(self):
        report = self.export()
        self.assertEqual(report["version"], 2)
        self.assertEqual(report["counts"]["sender_blocks"], 1)
        self.assertEqual(self.data()["tables"]["sender_blocks"], [
            dict(platform="old", account="555", sender_account="111", created=3.0)])

    def test_v1_eight_table_import_supplies_empty_sender_blocks(self):
        self.export()
        data = self.data()
        data["version"] = 1
        del data["tables"]["sender_blocks"]
        self.save(data)
        dry = migration.import_bundle(self.target, self.bundle, dry_run=True)
        report = migration.import_bundle(self.target, self.bundle)
        self.assertEqual(report["version"], 2)
        self.assertEqual(report["counts"]["sender_blocks"], {"inserted": 0, "skipped": 0})
        self.assertEqual(report["counts"], dry["counts"])
        self.assertEqual(report["inserted"], 6)
        self.assertEqual(self.target["optout"].execute("SELECT * FROM sender_blocks").fetchall(), [])

    def test_v1_unknown_or_missing_tables_still_rejected(self):
        def change(data):
            data["version"] = 1
            del data["tables"]["sender_blocks"]
            del data["tables"]["optouts"]
        self.assert_rejected(change)

    def test_v1_with_new_table_is_not_a_legacy_bundle(self):
        self.assert_rejected(lambda data: data.update(version=1))

    def test_v2_missing_sender_blocks_rejected(self):
        self.assert_rejected(lambda data: data["tables"].pop("sender_blocks"))

    def test_sender_block_mapping_and_conflicts_preserve_existing(self):
        self.insert(self.target, "optout", "sender_blocks", dict(platform="new", account="555", sender_account="111", created=99.0))
        self.insert(self.source, "optout", "sender_blocks", dict(platform="old", account="555", sender_account="222", created=4.0))
        self.export()
        dry = migration.import_bundle(self.target, self.bundle, {"old": "new"}, dry_run=True)
        report = migration.import_bundle(self.target, self.bundle, {"old": "new"})
        self.assertEqual(report["counts"]["sender_blocks"], {"inserted": 1, "skipped": 1})
        self.assertEqual(report["counts"], dry["counts"])
        self.assertEqual(self.target["optout"].execute(
            "SELECT platform,account,sender_account,created FROM sender_blocks ORDER BY sender_account").fetchall(),
            [("new", "555", "111", 99.0), ("new", "555", "222", 4.0)])

    def test_sender_block_invalid_rows_rejected_before_backup(self):
        self.export()
        original = self.data()
        for changes in ({"sender_account": ""}, {"sender_account": None}, {"sender_account": 123},
                        {"created": -1}, {"account": ""}, {"platform": ""}):
            with self.subTest(changes=changes):
                data = json.loads(json.dumps(original))
                data["tables"]["sender_blocks"][0].update(changes)
                self.save(data)
                with self.assertRaises(ValueError):
                    migration.import_bundle(self.target, self.bundle)
                self.assertFalse((self.root / "target" / "migration-backups").exists())

    def test_sender_block_failure_rolls_back_every_store(self):
        self.export()
        self.target["optout"].executescript(
            "CREATE TRIGGER deny_sender BEFORE INSERT ON sender_blocks BEGIN SELECT RAISE(ABORT, 'test failure'); END;")
        before = self.snapshot(self.target)
        with self.assertRaises(sqlite3.IntegrityError):
            migration.import_bundle(self.target, self.bundle)
        self.assertEqual(before, self.snapshot(self.target))

    def test_unknown_table_rejected(self):
        self.assert_rejected(lambda data: data["tables"].update({'tasks; DROP TABLE tasks;--': []}))

    def test_unknown_column_rejected(self):
        self.assert_rejected(lambda data: data["tables"]["blocked"][0].update({'x) VALUES(1);--': "evil"}))

    def test_missing_column_rejected(self):
        self.assert_rejected(lambda data: data["tables"]["tasks"][0].pop("owner_key"))

    def test_null_key_rejected(self):
        self.assert_rejected(lambda data: data["tables"]["tasks"][0].update(id=None))

    def test_bool_numeric_rejected(self):
        self.assert_rejected(lambda data: data["tables"]["tasks"][0].update(turns=True))

    def test_overflow_integer_rejected(self):
        self.assert_rejected(lambda data: data["tables"]["tasks"][0].update(turns=2**80))

    def test_timestamp_nan_rejected(self):
        self.assert_rejected(lambda data: data["tables"]["optouts"][0].update(created=float("nan")))

    def test_timestamp_negative_rejected(self):
        self.assert_rejected(lambda data: data["tables"]["groups"][0].update(synced=-1))

    def test_export_exclusive(self):
        self.export()
        previous = self.bundle.read_bytes()
        with self.assertRaises(FileExistsError):
            self.export()
        self.assertEqual(previous, self.bundle.read_bytes())
        self.assertEqual(list(self.root.glob(".migration-*")), [])
        if os.name != "nt":
            self.assertEqual(self.bundle.stat().st_mode & 0o777, 0o600)

    def test_size_limit_before_backup(self):
        self.export()
        with mock.patch.object(migration, "MAX_BUNDLE_BYTES", 10):
            with self.assertRaises(ValueError):
                migration.import_bundle(self.target, self.bundle)
        self.assertFalse((self.root / "target" / "migration-backups").exists())

    def test_duplicate_json_keys_and_version_rejected(self):
        self.bundle.write_text('{"version":1,"version":1,"tables":{}}')
        with self.assertRaises(ValueError):
            migration.import_bundle(self.target, self.bundle)
        self.bundle.unlink()
        self.assert_rejected(lambda data: data.update(version=True))

    def test_bad_transcript_no_replay_rows_still_validated(self):
        self.assert_rejected(lambda data: data["tables"]["outbox"][0].update(attempts="wrong"))
        self.bundle.unlink()
        self.assert_rejected(lambda data: data["tables"]["tasks"][0].update(transcript='[{"role":"user","text":"x","at":null}]'))

    def test_invalid_map_and_exact_not_partial(self):
        self.export()
        with self.assertRaises(ValueError):
            migration.import_bundle(self.target, self.bundle, {"old": "new:bad"})
        migration.import_bundle(self.target, self.bundle, {"ol": "new"})
        self.assertEqual(self.target["roster"].execute("SELECT platform FROM groups").fetchone()[0], "old")

    def test_exception_rolls_back_all_four_databases(self):
        self.export()
        # Failure in the last store must undo writes in the first three.
        self.target["optout"].executescript("CREATE TRIGGER deny_import BEFORE INSERT ON optouts BEGIN SELECT RAISE(ABORT, 'test failure'); END;")
        before = self.snapshot(self.target)
        with self.assertRaises(sqlite3.IntegrityError):
            migration.import_bundle(self.target, self.bundle)
        self.assertEqual(before, self.snapshot(self.target))
        self.assertEqual(len(list((self.root / "target" / "migration-backups").iterdir())), 1)

    def test_pending_transaction_rejected_without_committing(self):
        self.export()
        db = self.target["blocklist"]
        db.execute("INSERT INTO blocked VALUES('local','account','',1)")
        with self.assertRaises(ValueError):
            migration.import_bundle(self.target, self.bundle)
        self.assertTrue(db.in_transaction)
        db.rollback()


if __name__ == "__main__":
    unittest.main()
