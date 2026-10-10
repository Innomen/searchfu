import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import comparison
import replication


class ReplicationTests(unittest.TestCase):
    def fixture(self, root):
        paths = {key: root/key for key in ('db', 'ann', 'candidates')}
        root.mkdir(parents=True)
        for key in ('ann', 'candidates'):
            paths[key].mkdir()
        c = sqlite3.connect(paths['db'])
        c.executescript("PRAGMA journal_mode=WAL; CREATE TABLE index_state(key TEXT,value TEXT); INSERT INTO index_state VALUES('index_id','synthetic-identity'); CREATE TABLE evidence(text TEXT);")
        c.execute("INSERT INTO evidence VALUES('synthetic private evidence')")
        c.commit()
        (paths['ann']/'ann.meta.json').write_text(json.dumps({'index_id':'synthetic-identity'}))
        (paths['ann']/'block.bin').write_bytes(b'synthetic vector bytes')
        return paths, c

    def test_wal_snapshot_caches_rollback_copy_and_freshness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            src, c = self.fixture(root/'source')
            dst, old = self.fixture(root/'target')
            old.execute("INSERT INTO evidence VALUES('only in old replica')");old.commit();old.close()
            try:
                result = replication.replicate(src, dst)
                self.assertTrue(result['completed'])
                self.assertTrue(result['old_copy_preserved'])
                self.assertNotIn('synthetic private', str(result))
                with sqlite3.connect(dst['db']) as copied:
                    self.assertEqual(copied.execute('SELECT * FROM evidence').fetchall(), c.execute('SELECT * FROM evidence').fetchall())
                self.assertEqual((src['ann']/'block.bin').read_bytes(), (dst['ann']/'block.bin').read_bytes())
                backups = list(dst['db'].parent.glob('db.before-replica-*'))
                self.assertEqual(len(backups), 1)
                with sqlite3.connect(backups[0]) as saved:
                    self.assertEqual(saved.execute('SELECT count(*) FROM evidence').fetchone()[0], 2)
                cfg={'usb_db':str(dst['db'])}
                with patch.object(replication,'configured_paths',return_value=(src,dst)):
                    self.assertTrue(replication.snapshot_matches(cfg))
                    self.assertFalse(replication.snapshot_matches(cfg, root/'unrelated.db'))
                    c.execute("INSERT INTO evidence VALUES('newer authoritative evidence')");c.commit()
                    self.assertFalse(replication.snapshot_matches(cfg))
            finally:c.close()

    def test_failed_cache_copy_leaves_old_target_intact(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);src,c=self.fixture(root/'source');dst,old=self.fixture(root/'target');old.close()
            before=dst['db'].read_bytes()
            try:
                with patch.object(replication.shutil,'copytree',side_effect=OSError('synthetic failure')):
                    with self.assertRaises(OSError):replication.replicate(src,dst)
                self.assertEqual(dst['db'].read_bytes(),before)
                self.assertEqual(list(dst['db'].parent.glob('.searchfu-replica-*')),[])
            finally:c.close()

    def test_public_route_waits_for_lock_and_exports_only_aggregates(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);home=root/'home'
            src,c=self.fixture(home/'.local/share/searchfu');c.close()
            src['db'].rename(src['db'].parent/'drive.sqlite3')
            dst,old=self.fixture(root/'usb');old.close()
            cfg=root/'comparison.json'
            cfg.write_text(json.dumps({'enabled':True,'usb_db':str(dst['db']),'usb_ann':str(dst['ann']),'usb_candidates':str(dst['candidates']),'timeout_seconds':0}))
            env={**os.environ,'HOME':str(home),'SEARCHFU_COMPARISON_CONFIG':str(cfg),'SEARCHFU_METRICS_DB':str(root/'metrics.sqlite3'),'SEARCHFU_PY':sys.executable}
            result=subprocess.run(['bash','searchfu.sh','replicate','--comparison-target','--wait'],env=env,capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stdout)
            reports=[json.loads(line) for line in result.stdout.splitlines()]
            self.assertTrue(reports[-1]['completed'])
            self.assertNotIn(str(root),result.stdout)
            self.assertNotIn('synthetic-identity',result.stdout)
            self.assertEqual(result.stderr,'')

    def test_metric_labels_do_not_conflate_matching_and_unverified_snapshots(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'SEARCHFU_METRICS_DB':str(Path(directory)/'metrics.sqlite3')}):
            summary={'seconds':1,'count':1,'ids':['synthetic-id']}
            comparison.record('keyword','paired',summary,summary,snapshot_matched=True)
            comparison.record('keyword','paired',summary,summary)
            m=comparison.report()['modes']['keyword']
            self.assertEqual(m['matched_snapshot_pairs'],1)
            self.assertEqual(m['unmatched_snapshot_pairs'],1)
