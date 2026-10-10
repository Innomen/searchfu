import json, os, sqlite3, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import diagnostics

class DiagnosticTests(unittest.TestCase):
    def test_log_never_formats_private_exception_and_cli_is_closed(self):
        class PrivateError(Exception):
            def __str__(self):raise AssertionError('must never format exception')
        with tempfile.TemporaryDirectory() as root,patch.dict(os.environ,{'SEARCHFU_ERRORS_DB':str(Path(root)/'errors.sqlite3')}):
            diagnostics.record('gui','operation_failed',PrivateError('secret query /private/file'))
            diagnostics.record('gui','operation_failed',PrivateError('another secret'))
            report=diagnostics.report();self.assertEqual(report['errors'][0]['count'],2)
            self.assertNotIn('secret',json.dumps(report));self.assertEqual(diagnostics.log_path().stat().st_mode&0o777,0o600)
            with sqlite3.connect(diagnostics.log_path()) as c:
                raw=json.dumps(c.execute('SELECT * FROM errors').fetchall());self.assertNotIn('secret',raw);self.assertNotIn('/private',raw)
                c.execute("INSERT INTO errors VALUES('private title','private query','private path',1)")
            cli=subprocess.run(['bash','searchfu.sh','errors','--agent'],env={**os.environ,'SEARCHFU_PY':sys.executable},capture_output=True,text=True,timeout=10)
            self.assertEqual(cli.returncode,0);self.assertNotIn('private',cli.stdout);self.assertEqual(json.loads(cli.stdout),report)
            with self.assertRaises(ValueError):diagnostics.record('secret','operation_failed')
    def test_missing_log_read_is_nonmutating_and_test_overrides_isolate(self):
        with tempfile.TemporaryDirectory() as root,patch.dict(os.environ,{'SEARCHFU_METRICS_DB':str(Path(root)/'metrics.sqlite3')}):
            os.environ.pop('SEARCHFU_ERRORS_DB',None)
            self.assertEqual(diagnostics.log_path(),Path(root)/'errors.sqlite3')
            self.assertEqual(diagnostics.report()['errors'],[]);self.assertFalse(diagnostics.log_path().exists())
            diagnostics.record('comparison','missing_target');self.assertEqual(diagnostics.report()['errors'][0]['code'],'missing_target')
