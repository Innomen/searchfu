import json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch
from fixtures import fixture
import comparison, gui_control

class GuiControlTests(unittest.TestCase):
    def test_reply_validation_rejects_content_and_unbounded_values(self):
        from unittest.mock import MagicMock
        for reply in ({'query':'private'}, {'phase':'private'}, {'result_count':float('nan')}, {'running':'private'}):
            client=MagicMock();client.__enter__.return_value=client
            client.recv.return_value=json.dumps(reply).encode()+b'\n'
            with patch('gui_control.socket.socket',return_value=client):
                with self.assertRaisesRegex(RuntimeError,'^invalid_control_reply$'):
                    gui_control.request({'action':'status'})
    def test_public_control_search_status_and_closed_reply(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);db,ann,_=fixture(root/'index',rows=20,files=5)
            env={**os.environ,'QT_QPA_PLATFORM':'offscreen','SEARCHFU_DB':str(db),'SEARCHFU_ANN_DIR':str(ann),'SEARCHFU_COMPARISON_CONFIG':str(root/'comparison.json'),'SEARCHFU_METRICS_DB':str(root/'metrics.sqlite3'),'SEARCHFU_COLLECTIONS':str(root/'collections.json'),'SEARCHFU_GUI_SOCKET':str(root/'gui.sock')}
            with patch.dict(os.environ,env):
                cfg=comparison.load();cfg['enabled']=False;comparison.save(cfg)
                p=subprocess.Popen([sys.executable,'-c','import gui; gui.main('+repr(str(db))+')'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=env)
                try:
                    end=time.monotonic()+15
                    while not (root/'gui.sock').exists():
                        if p.poll() is not None:self.fail('desktop did not start')
                        if time.monotonic()>end:self.fail('control startup timed out')
                        time.sleep(.05)
                    self.assertFalse(gui_control.request({'action':'status'})['running'])
                    reply=gui_control.request({'action':'search','query':'synthetic','mode':'Keywords','refresh':False})
                    self.assertTrue(reply['accepted']);self.assertNotIn('synthetic',json.dumps(reply))
                    while time.monotonic()<end:
                        reply=gui_control.request({'action':'status'})
                        if not reply['running']:break
                        time.sleep(.05)
                    self.assertEqual(reply['outcome'],'done');self.assertGreater(reply['result_count'],0)
                    elapsed=reply['elapsed_seconds'];time.sleep(.1)
                    self.assertEqual(gui_control.request({'action':'status'})['elapsed_seconds'],elapsed)
                    cli=subprocess.run(['bash','searchfu.sh','gui-control','status'],env={**env,'SEARCHFU_PY':sys.executable},capture_output=True,text=True,timeout=5)
                    self.assertEqual(cli.returncode,0);self.assertNotIn(str(db),cli.stdout)
                    self.assertEqual((root/'gui.sock').stat().st_mode&0o777,0o600)
                    invalid=gui_control.request({'action':'search','query':'secret','db':'/private/secret'})
                    self.assertEqual(invalid,{'accepted':False,'error':'invalid_command'})
                finally:p.terminate();p.wait(timeout=10)
    def test_primary_timings_without_usb_and_pending_lock(self):
        import fcntl
        with tempfile.TemporaryDirectory() as root,patch.dict(os.environ,{'SEARCHFU_METRICS_DB':str(Path(root)/'metrics.sqlite3')}):
            comparison.record_primary('keyword','primary_timeout',{'seconds':3,'count':0})
            comparison.record('keyword','primary_timeout')
            v=comparison.report()['modes']['keyword'];self.assertEqual(v['primary_timeouts'],1);self.assertEqual(v['primary_timeout'],1);self.assertEqual(v['primary_seconds_sum'],3)
            lock=Path(root)/'comparison.lock'
            with lock.open('w') as f:
                fcntl.flock(f,fcntl.LOCK_EX)
                self.assertTrue(comparison.report()['activity']['active'])
            self.assertFalse(comparison.report()['activity']['active'])
