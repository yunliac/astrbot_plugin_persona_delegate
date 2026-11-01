import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('errors_under_test', Path(__file__).resolve().parents[1] / 'error_reports.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class ErrorReportsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'reports'
        self.reports = m.ErrorReports(self.root)

    def cause(self):
        try:
            raise ConnectionError('api_key=SECRET-KEY private chat 123456789 http://secret.example')
        except ConnectionError as exc:
            return self.reports.capture('network', exc)

    def test_default_capture_private_data_removed_and_selfcheck_passes(self):
        path = self.cause()
        text = path.read_text()
        data = json.loads(text)
        for secret in ('SECRET-KEY', 'private chat', '123456789', 'secret.example', str(self.root.parent)):
            self.assertNotIn(secret, text)
        self.assertTrue(data['self_check']['passed'])
        self.assertEqual(data['exception_type'], 'ConnectionError')
        self.assertTrue(data['frames'])
        self.assertTrue(data['troubleshooting'])

    def test_disabled_still_manual_export(self):
        self.reports.enabled = False
        self.assertIsNone(self.cause())
        data = json.loads(self.reports.manual_export().read_text())
        self.assertEqual(data['status'], 'no_records')
        self.assertTrue(data['self_check']['passed'])

    def test_same_error_rate_limited(self):
        self.assertIsNotNone(self.cause())
        self.assertIsNone(self.cause())
        self.assertEqual(len(self.reports.list_reports()), 1)

    def test_retention_preserves_unrelated_files(self):
        self.reports.max_reports = 2
        self.root.mkdir()
        sentinel = self.root / 'notes.json'
        sentinel.write_text('preserve')
        for i in range(4):
            self.reports.capture('stage'+str(i), RuntimeError('secret'))
        self.assertEqual(len(self.reports.list_reports()), 2)
        self.assertEqual(sentinel.read_text(), 'preserve')

    def test_chain_contains_types_not_messages(self):
        try:
            try:
                raise PermissionError('FIRST-SECRET')
            except PermissionError as exc:
                raise RuntimeError('SECOND-SECRET') from exc
        except RuntimeError as exc:
            path = self.reports.capture('chain', exc)
        text = path.read_text()
        self.assertNotIn('FIRST-SECRET', text)
        self.assertNotIn('SECOND-SECRET', text)
        self.assertEqual(json.loads(text)['chain'][0]['exception_type'], 'PermissionError')

    def test_io_failure_cannot_break_caller(self):
        with patch.object(self.reports, '_publish', side_effect=OSError('disk full')):
            self.assertIsNone(self.cause())

    def test_manual_skips_malformed_and_reconstructs_allowed_fields(self):
        path = self.cause()
        data = json.loads(path.read_text())
        data['message'] = 'SECRET-EXTRA'
        path.write_text(json.dumps(data))
        (self.root / 'error-broken.json').write_text('broken')
        exported = self.reports.manual_export().read_text()
        self.assertNotIn('SECRET-EXTRA', exported)
        result = json.loads(exported)
        self.assertEqual(len(result['reports']), 1)
        self.assertTrue(result['self_check']['passed'])

    def test_symlink_report_never_loaded_or_deleted(self):
        self.root.mkdir()
        target = self.root.parent / 'external'
        target.write_text('PRIVATE')
        link = self.root / 'error-link.json'
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest('symlink privilege unavailable')
        self.assertEqual(self.reports.list_reports(), [])
        self.assertNotIn('PRIVATE', self.reports.manual_export().read_text())
        self.assertEqual(target.read_text(), 'PRIVATE')
