import os, sqlite3, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from fixtures import fixture
from retrieval import retrieve, _lexical
from search import connect

class LegacyKeywordTests(unittest.TestCase):
    def test_legacy_readonly_query_no_migration(self):
        with tempfile.TemporaryDirectory() as root:
            db,_,_=fixture(Path(root),rows=20,files=5)
            with sqlite3.connect(db) as c:
                c.execute('DROP TRIGGER chunks_water');c.execute('DROP TABLE index_state')
                c.execute('DROP INDEX files_content_hash')
                for name in ('content_hash','source_device','extractor'):c.execute('ALTER TABLE files DROP COLUMN '+name)
            before=db.read_bytes()
            events=list(retrieve(db,'synthetic',fts_only=True,refresh=False,comparison=False))
            self.assertEqual(events[-1]['stage'],'done');self.assertTrue(events[-1]['results']);self.assertEqual(before,db.read_bytes())
            with sqlite3.connect(db) as c:self.assertFalse(c.execute("SELECT 1 FROM sqlite_master WHERE name='index_state'").fetchone())
            # Missing metadata must not widen a pinned-filesystem view.
            with connect(db,readonly=True) as c:
                self.assertFalse(c.execute('SELECT 1 FROM files WHERE source_device IN (?)',(123,)).fetchone())
    def test_native_rank_matches_bm25_and_does_not_sort_joined_rows(self):
        with tempfile.TemporaryDirectory() as root:
            db,_,_=fixture(Path(root),rows=400,files=100)
            with sqlite3.connect(db) as c:
                oracle={p:(cid,p,k,mt,mime,-float(score)) for cid,p,k,mt,mime,score in reversed(c.execute('SELECT c.id,f.path,f.kind,f.mtime,f.mime,bm25(chunks_fts) FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid JOIN files f ON f.id=c.file_id WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts)',('synthetic',)).fetchall())}
                actual=_lexical(c,'synthetic',150,[],[],False)
                self.assertEqual(set(actual),set(oracle))
                for p in actual:self.assertEqual(actual[p][-1],oracle[p][-1])
                plan=c.execute("EXPLAIN QUERY PLAN SELECT c.id FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid JOIN files f ON f.id=c.file_id WHERE chunks_fts MATCH ? AND chunks_fts.rank MATCH 'bm25()' ORDER BY chunks_fts.rank LIMIT 300",('synthetic',)).fetchall()
                self.assertFalse(any('TEMP B-TREE' in row[-1] for row in plan))
