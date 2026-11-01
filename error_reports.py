"""Privacy-first, standard-library-only plugin error diagnostics.

The caller supplies ``data_dir / 'error-reports'`` as root. Exception messages,
source text, locals, request payloads and configuration are never inspected.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time
import uuid

_VERSION = 1
_PLUGIN_VERSION = "1.0.0beta"
_MAX_FRAMES = 24
_MAX_CHAIN = 8
_MAX_LOAD = 128 * 1024
_MAX_EXPORT = 1024 * 1024
_SAFE = re.compile(r"[^A-Za-z0-9_.<>-]+")
_DIGITS = re.compile(r"\d{5,}")
_REPORT_NAME = re.compile(r"error-[0-9a-f]{32}\.json\Z")
_FORBIDDEN = {"message", "locals", "body"}


def _safe(value: object, limit: int = 96) -> str:
    if not isinstance(value, str):
        return "unknown"
    # Redact before truncating: a long identifier must never leave a prefix.
    return _SAFE.sub("_", _DIGITS.sub("REDACTED", value))[:limit] or "unknown"


def _frames(tb) -> list[dict]:
    result = deque(maxlen=_MAX_FRAMES)
    while tb is not None:
        code = tb.tb_frame.f_code
        result.append({"filename": _safe(ntpath.basename(code.co_filename)),
                       "function": _safe(code.co_name), "line": max(0, tb.tb_lineno)})
        tb = tb.tb_next
    return list(result)


def _tips(kind: str) -> list[str]:
    name = kind.lower()
    if "timeout" in name:
        return ["Check service availability and timeout limits. Inspect execution status before retrying to avoid duplicate actions."]
    if "permission" in name:
        return ["Check data-directory permissions and service account access."]
    if "connection" in name or "network" in name:
        return ["Check network connectivity and upstream service availability."]
    if "filenotfound" in name:
        return ["Check required files exist and the configured data directory is accessible."]
    return ["Inspect task status and service logs before retrying; actions may already have executed. Share this sanitized report with the maintainer if needed."]


def _valid_frames(frames: object) -> bool:
    return (isinstance(frames, list) and len(frames) <= _MAX_FRAMES
            and all(isinstance(f, dict) and set(f) == {"filename", "function", "line"}
                    and all(isinstance(f[k], str) and f[k] == _safe(f[k])
                            for k in ("filename", "function"))
                    and type(f["line"]) is int and 0 <= f["line"] <= 10**9
                    for f in frames))


def _self_check(report: dict) -> dict:
    def clean(value):
        if isinstance(value, dict):
            return all(str(k).lower() not in _FORBIDDEN and clean(v) for k, v in value.items())
        if isinstance(value, list):
            return all(clean(v) for v in value)
        return True
    chain = report.get("chain", [])
    frames_ok = (_valid_frames(report.get("frames")) and isinstance(chain, list)
                 and len(chain) <= _MAX_CHAIN
                 and all(isinstance(c, dict) and set(c) == {"exception_type", "frames"}
                         and _valid_frames(c.get("frames")) for c in chain))
    return {"passed": clean(report) and frames_ok,
            "no_sensitive_keys": clean(report), "frames_valid": frames_ok}


class ErrorReports:
    """Bounded on-disk reports; automatic collection is best-effort only."""

    def __init__(self, root, enabled: bool = True, max_reports: int = 100):
        self.root = Path(root).absolute()
        self.enabled = enabled
        self.max_reports = max(1, int(max_reports))
        self._history = deque(maxlen=50)
        self._lock = threading.RLock()
        self._capturing = False

    def _check_root(self, create: bool = False) -> None:
        # Reject symlinks/junctions anywhere in the supplied path, not only leaves.
        for path in reversed((self.root, *self.root.parents)):
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise OSError("Diagnostic directory must not traverse links")
        if create:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.exists():
            if not self.root.is_dir():
                raise OSError("Diagnostic root is not a directory")
            if create:
                os.chmod(self.root, 0o700)

    def list_reports(self) -> list[Path]:
        """List only regular non-link error-*.json files, newest first."""
        self._check_root()
        if not self.root.exists():
            return []
        found = []
        for path in self.root.iterdir():
            if not (path.name.startswith("error-") and path.name.endswith(".json")):
                continue
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and not path.is_symlink():
                found.append((info.st_mtime_ns, path.name, path))
        return [entry[2] for entry in sorted(found, reverse=True)]

    def _publish(self, prefix: str, report: dict) -> Path:
        self._check_root(create=True)
        data = json.dumps(report, ensure_ascii=True, indent=2).encode("utf-8")
        if len(data) > _MAX_EXPORT:
            raise ValueError("Diagnostic export exceeds size limit")
        ident = uuid.uuid4().hex
        temporary = self.root / (".pending-" + ident)
        final = self.root / (prefix + ident + ".json")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(temporary, flags, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._check_root()
            # A hard link atomically publishes a complete file, and never replaces
            # any preexisting destination (including symlinks).
            os.link(temporary, final)
            return final
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def capture(self, stage: str, exc: BaseException | None = None) -> Path | None:
        """Capture a sanitized stack, or return None; never propagate failures."""
        try:
            if not self.enabled:
                return None
            if exc is None:
                exc = sys.exception()
            if not isinstance(exc, BaseException):
                return None
            with self._lock:
                if self._capturing:
                    return None
                self._capturing = True
                try:
                    report = {"version": _VERSION, "plugin_version": _PLUGIN_VERSION,
                              "id": uuid.uuid4().hex,
                              "timestamp": datetime.now(timezone.utc).isoformat(),
                              "stage": _safe(stage, 64),
                              "exception_type": _safe(type(exc).__name__),
                              "frames": _frames(exc.__traceback__), "chain": []}
                    seen = {id(exc)}
                    current = exc
                    for _ in range(_MAX_CHAIN):
                        current = (current.__cause__ if current.__cause__ is not None else
                                   None if current.__suppress_context__ else current.__context__)
                        if current is None or id(current) in seen:
                            break
                        seen.add(id(current))
                        report["chain"].append({"exception_type": _safe(type(current).__name__),
                                                "frames": _frames(current.__traceback__)})
                    fingerprint = hashlib.sha256(json.dumps(
                        [report[k] for k in ("stage", "exception_type", "frames", "chain")],
                        sort_keys=True).encode()).digest()
                    now = time.monotonic()
                    if any(key == fingerprint and now - stamp < 60 for stamp, key in self._history):
                        return None
                    report["troubleshooting"] = _tips(report["exception_type"])
                    report["self_check"] = _self_check(report)
                    path = self._publish("error-", report)
                    self._history.append((now, fingerprint))
                    for old in self.list_reports()[self.max_reports:]:
                        self._check_root()
                        if stat.S_ISREG(old.lstat().st_mode) and not old.is_symlink():
                            old.unlink()
                    return path
                finally:
                    self._capturing = False
        except Exception:
            return None

    def _load(self, path: Path) -> dict | None:
        """Read bounded JSON and reconstruct *only* the known report schema."""
        try:
            self._check_root()
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_LOAD:
                return None
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    return None
                data = json.loads(stream.read(_MAX_LOAD + 1))
            if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
                return None
            ident = data.get("id")
            if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{32}", ident):
                return None
            timestamp = data.get("timestamp")
            if not isinstance(timestamp, str) or len(timestamp) > 40:
                return None
            parsed = datetime.fromisoformat(timestamp)
            if parsed.tzinfo is None:
                return None
            def safe_frames(value):
                if not isinstance(value, list):
                    raise ValueError("Invalid frames")
                result = []
                for frame in value[-_MAX_FRAMES:]:
                    if not isinstance(frame, dict) or type(frame.get("line")) is not int:
                        raise ValueError("Invalid frame")
                    result.append({"filename": _safe(ntpath.basename(frame.get("filename", ""))),
                                   "function": _safe(frame.get("function")),
                                   "line": min(10**9, max(0, frame["line"]))})
                return result
            chain = data.get("chain", [])
            if not isinstance(chain, list) or any(not isinstance(c, dict) for c in chain):
                return None
            report = {"version": 1, "plugin_version": _PLUGIN_VERSION, "id": ident,
                      "timestamp": parsed.astimezone(timezone.utc).isoformat(),
                      "stage": _safe(data.get("stage"), 64),
                      "exception_type": _safe(data.get("exception_type")),
                      "frames": safe_frames(data.get("frames")),
                      "chain": [{"exception_type": _safe(c.get("exception_type")),
                                 "frames": safe_frames(c.get("frames"))} for c in chain[:_MAX_CHAIN]]}
            report["troubleshooting"] = _tips(report["exception_type"])
            report["self_check"] = _self_check(report)
            return report
        except (OSError, ValueError, TypeError, OverflowError, RecursionError):
            return None

    def manual_export(self) -> Path:
        """Export latest 20 valid reports (<=1 MiB), even when disabled.

        Only malformed individual reports are skipped. Directory/write failures
        propagate so the caller can notify the requesting administrator.
        """
        with self._lock:
            self._check_root(create=True)
            reports = []
            export = {"version": 1, "plugin_version": _PLUGIN_VERSION,
                      "id": uuid.uuid4().hex,
                      "timestamp": datetime.now(timezone.utc).isoformat(),
                      "kind": "error_report_export", "status": "no_records", "reports": reports,
                      "troubleshooting": ["No recorded errors are available."],
                      "self_check": {"passed": True, "no_sensitive_keys": True, "frames_valid": True}}
            for path in self.list_reports():
                report = self._load(path)
                if report is None:
                    continue
                reports.append(report)
                if len(json.dumps(export, ensure_ascii=True, indent=2).encode()) > _MAX_EXPORT - 4096:
                    reports.pop()
                    break
                if len(reports) == 20:
                    break
            if reports:
                export["status"] = "records_available"
                export["troubleshooting"] = sorted({tip for r in reports for tip in r["troubleshooting"]})
            checks = [r["self_check"] for r in reports]
            export["self_check"] = {key: all(c[key] for c in checks)
                                    for key in ("passed", "no_sensitive_keys", "frames_valid")}
            return self._publish("export-", export)
