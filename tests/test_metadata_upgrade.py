import json
import sqlite3
import tempfile
import unittest
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
from fixtures import fixture
from metadata_upgrade import upgrade
from comparison import scope_supported
from retrieval import _metadata


class MetadataUpgradeTests(unittest.TestCase):
    def test_public_command_exports_only_closed_aggregates(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);db,ann,_=fixture(root,rows=5,files=1)
            config=root/'comparison.json'
            config.write_text(json.dumps({'enabled':True,'usb_db':str(db),'usb_ann':str(ann),'usb_candidates':'','timeout_seconds':10}))
            env={**os.environ,'SEARCHFU_COMPARISON_CONFIG':str(config),'SEARCHFU_METRICS_DB':str(root/'state/metrics.sqlite3'),'SEARCHFU_PY':sys.executable}
            run=subprocess.run(['bash',str(Path(__file__).resolve().parents[1]/'searchfu.sh'),'metadata-upgrade','--comparison-target'],env=env,capture_output=True,text=True,timeout=10)
            self.assertEqual(run.returncode,0);self.assertEqual(run.stderr,'')
            self.assertNotIn(str(root),run.stdout);self.assertNotIn('synthetic-corpus',run.stdout)
            allowed={'checked','updated','unavailable','elapsed_seconds','completed','remaining_unknown','complete','cache_identity_preserved'}
            for line in run.stdout.splitlines():self.assertFalse(set(json.loads(line))-allowed)
    def test_preserves_stored_content_cache_and_identity_and_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);db,ann,_=fixture(root,rows=20,files=2)
            existing=root/'present.txt';existing.write_text('not read by migration')
            missing=root/'missing.txt'
            with sqlite3.connect(db) as c:
                c.execute('UPDATE files SET path=? WHERE id=1',(str(existing),))
                c.execute('UPDATE files SET path=? WHERE id=2',(str(missing),))
                before=c.execute('SELECT * FROM chunks').fetchall()
                c.execute('DROP TRIGGER chunks_water');c.execute('DROP TABLE index_state')
                c.execute('DROP INDEX files_content_hash')
                for name in ('content_hash','source_device','extractor'):c.execute('ALTER TABLE files DROP COLUMN '+name)
            cache={p:p.read_bytes() for p in ann.rglob('*') if p.is_file()}
            report=upgrade(db,ann,batch_size=1)
            self.assertEqual(report['updated'],1);self.assertEqual(report['remaining_unknown'],1)
            with sqlite3.connect(db) as c:
                self.assertEqual(before,c.execute('SELECT * FROM chunks').fetchall())
                self.assertEqual(c.execute('SELECT source_device FROM files WHERE id=1').fetchone()[0],existing.stat().st_dev)
                self.assertEqual(c.execute('SELECT count(*) FROM files WHERE content_hash IS NOT NULL').fetchone()[0],0)
                _metadata(ann,c)
            self.assertEqual(cache,{p:p.read_bytes() for p in ann.rglob('*') if p.is_file()})
            config={'collections':{'fixture':{'root':str(root),'one_filesystem':True}}}
            with patch('collections_config.load',return_value=config):
                self.assertFalse(scope_supported(db,{'scopes':['fixture']}))
            missing.write_text('later available')
            report=upgrade(db,ann)
            self.assertEqual(report['checked'],1);self.assertTrue(report['complete'])
            with patch('collections_config.load',return_value=config):
                self.assertTrue(scope_supported(db,{'scopes':['fixture']}))
            self.assertEqual(upgrade(db,ann)['checked'],0)

    def test_mismatched_identity_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            db,ann,_=fixture(Path(directory),rows=5,files=1)
            p=ann/'ann.meta.json';meta=json.loads(p.read_text());meta['index_id']='different';p.write_text(json.dumps(meta))
            with self.assertRaises(ValueError):upgrade(db,ann)
            with sqlite3.connect(db) as c:
                self.assertFalse(c.execute("SELECT 1 FROM index_state WHERE key='metadata_device_upgrade'").fetchone())
