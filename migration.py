"""Standard-library, offline logical migration of delegate SQLite stores.

Public API: export_bundle(stores, output_path),
import_bundle(stores, input_path, platform_map=None, dry_run=False).
``stores`` is exactly a dict of tasks/roster/blocklist/optout connections.
Callers must quiesce the engine and roster sync while migrating; connections
must have no pending transaction. Import requires four distinct persistent DBs.

An import uses one separate connection and one ATTACH transaction, rolling back
all four stores on exceptions. SQLite does NOT promise power-loss atomicity
across attached databases in WAL mode. We deliberately do not change live
journal modes; the four SQLite backups provide recovery from a crash. Exports
and backups contain private messages, persona prompts and account identifiers.
POSIX mode 0600/0700 is requested; Windows ACL policy still applies.
"""
from __future__ import annotations

import datetime
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile

__all__ = ["export_bundle", "import_bundle"]
VERSION = 2
MAX_BUNDLE_BYTES = 32 * 1024 * 1024
_TABLES = {
    "tasks": ("tasks", "outbox", "followups"),
    "roster": ("groups", "members", "aliases"),
    "blocklist": ("blocked",),
    "optout": ("optouts", "sender_blocks"),
}
_PREFIX_FIELDS = frozenset(("owner_key", "target", "origin", "report_to", "added_by"))
_TIMESTAMPS = frozenset(("created", "updated", "deadline", "next_attempt", "until", "synced", "added_at"))
_STATUSES = frozenset(("waiting", "sending", "completed", "failed", "cancelled", "timeout", "interrupted"))


def _quote(name):
    # Names only ever originate in the fixed table list or trusted PRAGMA data.
    return '"' + name.replace('"', '""') + '"'


def _stores(stores):
    if type(stores) is not dict or set(stores) != set(_TABLES):
        raise ValueError("stores must contain exactly tasks, roster, blocklist, optout")
    schemas, paths = {}, {}
    for store, tables in _TABLES.items():
        db = stores[store]
        if not isinstance(db, sqlite3.Connection):
            raise ValueError("store values must be SQLite connections")
        if db.in_transaction:
            raise ValueError("commit or rollback pending store transactions first")
        paths[store] = next((row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main"), "")
        for table in tables:
            columns = [tuple(row) for row in db.execute("PRAGMA table_info(" + _quote(table) + ")")]
            if not columns or not any(c[5] for c in columns):
                raise ValueError("missing table or primary key: " + table)
            if any(c[2].upper() not in ("TEXT", "INTEGER", "REAL") for c in columns):
                raise ValueError("unsupported schema type: " + table)
            schemas[table] = columns
    return schemas, paths


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def _json(text):
    try:
        return json.loads(text, object_pairs_hook=_pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite JSON number")))
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise ValueError("invalid JSON") from exc


def _validate_row(table, row, columns):
    if type(row) is not dict or set(row) != {c[1] for c in columns}:
        raise ValueError("wrong row columns: " + table)
    for _, name, kind, notnull, _, pk in columns:
        value = row[name]
        if value is None:
            if notnull or pk:
                raise ValueError("null required column: " + table + "." + name)
            continue
        kind = kind.upper()
        if kind == "TEXT" and type(value) is not str:
            raise ValueError("expected text: " + name)
        if kind == "INTEGER" and (type(value) is not int or not -(2**63) <= value < 2**63):
            raise ValueError("expected SQLite integer: " + name)
        if kind == "REAL" and not _number(value):
            raise ValueError("expected finite number: " + name)
        if name in _TIMESTAMPS and (not _number(value) or value < 0):
            raise ValueError("expected nonnegative finite timestamp: " + name)
        if kind == "INTEGER" and value < 0:
            raise ValueError("negative counter: " + name)
        if name in ("id", "platform", "account", "sender_account", "qq") and value in ("", 0):
            raise ValueError("empty identity: " + name)
    if table == "tasks":
        if row["status"] not in _STATUSES:
            raise ValueError("invalid task status")
        transcript = _json(row["transcript"])
        if type(transcript) is not list:
            raise ValueError("transcript must encode a list")
        for entry in transcript:
            if (type(entry) is not dict or not {"role", "text", "at"} <= set(entry)
                    or set(entry) - {"role", "text", "at", "delivery"}
                    or type(entry["role"]) is not str or not entry["role"]
                    or type(entry["text"]) is not str
                    or not _number(entry["at"]) or entry["at"] < 0
                    or ("delivery" in entry and type(entry["delivery"]) is not str)):
                raise ValueError("invalid transcript entry")
    if table == "members" and row["source"] not in ("sync", "mention"):
        raise ValueError("invalid member source")


def _validate(bundle, schemas):
    if (type(bundle) is not dict or set(bundle) != {"version", "tables"}
            or type(bundle["version"]) is not int or bundle["version"] != VERSION
            or type(bundle["tables"]) is not dict or set(bundle["tables"]) != set(schemas)):
        raise ValueError("unsupported bundle structure or version")
    for table, columns in schemas.items():
        rows = bundle["tables"][table]
        if type(rows) is not list:
            raise ValueError("table rows must be lists")
        for row in rows:
            _validate_row(table, row, columns)


def _platforms(tables):
    result = set()
    for rows in tables.values():
        for row in rows:
            if "platform" in row:
                result.add(row["platform"])
            for name in _PREFIX_FIELDS & row.keys():
                prefix, sep, _ = row[name].partition(":")
                if sep:
                    result.add(prefix)
    return sorted(result)


def _report(tables, counts, backup_path=None, dry_run=False):
    return {"version": VERSION, "counts": counts,
            "inserted": sum(c["inserted"] for c in counts.values()),
            "skipped": sum(c["skipped"] for c in counts.values()),
            "platforms": _platforms(tables), "backup_path": backup_path, "dry_run": dry_run}


def export_bundle(stores, output_path):
    """Export all nine durable tables; atomically publish a NEW private JSON file.

    Existing output paths are never replaced (FileExistsError). Return version,
    per-table exported counts, platforms and the output path.
    """
    schemas, _ = _stores(stores)
    tables = {}
    for store, names in _TABLES.items():
        for table in names:
            columns = [c[1] for c in schemas[table]]
            order = [c[1] for c in sorted(schemas[table], key=lambda c: c[5]) if c[5]]
            sql = "SELECT " + ",".join(map(_quote, columns)) + " FROM " + _quote(table)
            sql += " ORDER BY " + ",".join(map(_quote, order))
            tables[table] = [dict(zip(columns, row)) for row in stores[store].execute(sql)]
    bundle = {"version": VERSION, "tables": tables}
    _validate(bundle, schemas)
    payload = json.dumps(bundle, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_BUNDLE_BYTES:
        raise ValueError("bundle exceeds 32 MiB")
    output = Path(output_path).absolute()
    fd, temporary = tempfile.mkstemp(prefix=".migration-", suffix=".tmp", dir=output.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.chmod(temporary, 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # Unlike replace(), hard-link publication is atomic AND exclusive.
        os.link(temporary, output)
    finally:
        os.unlink(temporary)
    return {"version": VERSION, "counts": {t: len(r) for t, r in tables.items()},
            "platforms": _platforms(tables), "output_path": str(output)}


def _mapped(tables, platform_map):
    if platform_map is None:
        platform_map = {}
    if (type(platform_map) is not dict or any(type(k) is not str or type(v) is not str
            or not k or not v or ":" in k or ":" in v or k == "*" or v == "*"
            for k, v in platform_map.items())):
        raise ValueError("platform_map must map nonempty platform names without colons or wildcards")
    result = {}
    for table, rows in tables.items():
        result[table] = []
        for original in rows:
            row = original.copy()
            if "platform" in row:
                row["platform"] = platform_map.get(row["platform"], row["platform"])
            for name in _PREFIX_FIELDS & row.keys():
                prefix, sep, rest = row[name].partition(":")
                if sep and prefix in platform_map:
                    row[name] = platform_map[prefix] + ":" + rest
            if table == "tasks" and row["status"] in ("sending", "waiting"):
                row["status"] = "interrupted"
            if table == "members":
                row["source"] = "mention"
            result[table].append(row)
    return result


def _dry_counts(stores, tables, schemas):
    counts = {}
    for store, names in _TABLES.items():
        for table in names:
            rows = tables[table]
            if table in ("outbox", "followups"):
                counts[table] = {"inserted": 0, "skipped": len(rows)}
                continue
            keys = [c[1] for c in sorted(schemas[table], key=lambda c: c[5]) if c[5]]
            existing = {tuple(row) for row in stores[store].execute(
                "SELECT " + ",".join(map(_quote, keys)) + " FROM " + _quote(table))}
            inserted = 0
            for row in rows:
                key = tuple(row[k] for k in keys)
                if key not in existing:
                    existing.add(key)
                    inserted += 1
            counts[table] = {"inserted": inserted, "skipped": len(rows) - inserted}
    return counts


def import_bundle(stores, input_path, platform_map=None, dry_run=False):
    """Validate fully, back up, then merge without replacing existing rows.

    IDs are preserved verbatim; active imported tasks become interrupted. All
    imported members are mention-only until a fresh local roster sync. Outbox
    and followups are validated/exported but always skipped on import. Counts
    include those intentional skips. A dry run predicts primary-key conflicts
    with no writes or backups. Return counts, totals, mapped platforms,
    backup_path (None for dry run), version and dry_run.
    """
    if type(dry_run) is not bool:
        raise ValueError("dry_run must be bool")
    schemas, paths = _stores(stores)
    with open(input_path, "rb") as stream:
        payload = stream.read(MAX_BUNDLE_BYTES + 1)
    if len(payload) > MAX_BUNDLE_BYTES:
        raise ValueError("bundle exceeds 32 MiB")
    bundle = _json(payload)
    # Version 1 contained exactly the original eight tables. Only that known
    # structure is upgraded; malformed/unknown tables still fail validation.
    if (type(bundle) is dict and set(bundle) == {"version", "tables"}
            and type(bundle["version"]) is int and bundle["version"] == 1
            and type(bundle["tables"]) is dict
            and set(bundle["tables"]) == set(schemas) - {"sender_blocks"}):
        bundle = {"version": VERSION, "tables": {**bundle["tables"], "sender_blocks": []}}
    _validate(bundle, schemas)  # Nothing, including backups, is written before this.
    tables = _mapped(bundle["tables"], platform_map)
    _validate({"version": VERSION, "tables": tables}, schemas)
    if dry_run:
        return _report(tables, _dry_counts(stores, tables, schemas), dry_run=True)
    if any(not p or not Path(p).is_file() for p in paths.values()):
        raise ValueError("import requires persistent database files")
    resolved = [str(Path(p).resolve()) for p in paths.values()]
    if len(set(os.path.normcase(p) for p in resolved)) != 4:
        raise ValueError("import requires four distinct database files")
    root = Path(paths["tasks"]).resolve().parent / "migration-backups"
    root.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ-")
    backup_dir = Path(tempfile.mkdtemp(prefix=stamp, dir=root))
    os.chmod(backup_dir, 0o700)
    # SQLite backup includes committed WAL content without copying live files.
    for store in _TABLES:
        path = backup_dir / (store + ".sqlite3")
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        destination = sqlite3.connect(str(path))
        try:
            stores[store].backup(destination)
        finally:
            destination.close()
    coordinator = sqlite3.connect(str(Path(paths["tasks"]).resolve()))
    counts = {}
    try:
        for store, path in paths.items():
            if store != "tasks":
                coordinator.execute("ATTACH DATABASE ? AS " + _quote(store), (str(Path(path).resolve()),))
        coordinator.execute("PRAGMA synchronous=FULL")
        coordinator.execute("BEGIN IMMEDIATE")
        for store, names in _TABLES.items():
            for table in names:
                rows = tables[table]
                inserted = 0
                if table not in ("outbox", "followups"):
                    columns = [c[1] for c in schemas[table]]
                    sql = ("INSERT OR IGNORE INTO " + _quote("main" if store == "tasks" else store) + "." + _quote(table)
                           + " (" + ",".join(map(_quote, columns)) + ") VALUES ("
                           + ",".join("?" for _ in columns) + ")")
                    for row in rows:
                        inserted += coordinator.execute(sql, tuple(row[c] for c in columns)).rowcount
                counts[table] = {"inserted": inserted, "skipped": len(rows) - inserted}
        coordinator.commit()
    except BaseException:
        coordinator.rollback()
        raise
    finally:
        coordinator.close()
    return _report(tables, counts, str(backup_dir))
