import os,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import search,sqlite_import
from retrieval import retrieve

class LegacyIndexTests(unittest.TestCase):
    def test_copy_migrates_legacy_columns_preserving_evidence_and_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'old.sqlite3';target=root/'new.sqlite3'
            fixture=root/'unavailable.txt';fixture.write_text('Synthetic legacy cancellation evidence.')
            c=search.connect(source)
            c.execute('INSERT INTO files(id,path,root,kind) VALUES(7,?,?,?)',(str(root/'unavailable.txt'),str(root),'text'))
            blob=search.pack([1.0]+[0.0]*383)
            c.execute('INSERT INTO chunks VALUES(31,7,0,?,?)',('Synthetic legacy cancellation evidence.',blob));c.commit()
            c.execute('DROP INDEX files_content_hash')
            for col in ('content_hash','source_device','extractor'):c.execute('ALTER TABLE files DROP COLUMN '+col)
            c.commit();c.close()
            before=source.read_bytes()
            with patch('os.walk',side_effect=AssertionError('walk')),patch('os.scandir',side_effect=AssertionError('scan')),patch.object(search.Models,'text_vecs',side_effect=AssertionError('embed')):
                result=sqlite_import.import_index(source,target)
                self.assertEqual(result['chunks_imported'],1)
                self.assertFalse(result['source_tree_walked']);self.assertFalse(result['reembedded'])
                self.assertEqual(len(list(retrieve(target,'cancellation',fts_only=True))[-1]['results']),1)
            self.assertEqual(source.read_bytes(),before)
            c=search.connect(target,readonly=True)
            try:self.assertEqual(c.execute('SELECT id,embedding FROM chunks').fetchone(),(31,blob))
            finally:c.close()
            with self.assertRaises(ValueError):sqlite_import.import_index(source,target)

    def test_unchanged_legacy_signature_does_not_require_reembedding(self):
        import contextlib,io
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'corpus';root.mkdir();fixture=root/'fixture.txt';fixture.write_text('Synthetic legacy cancellation evidence. '*3)
            db=Path(tmp)/'index.sqlite3';c=search.connect(db)
            c.execute('INSERT INTO files(path,root,kind,content_sig,indexed_at) VALUES(?,?,?,?,?)',(str(fixture),str(root),'text',search.file_sig(fixture.stat()),1))
            fid=c.execute('SELECT id FROM files').fetchone()[0];blob=search.pack([1.0]+[0.0]*383)
            c.execute('INSERT INTO chunks VALUES(1,?,0,?,?)',(fid,'Synthetic legacy cancellation evidence.',blob));c.commit();c.close()
            with patch.object(search.Models,'text_vecs',side_effect=AssertionError('reembedded')),patch.dict(os.environ,{'SEARCHFU_CUDA':'0','SEARCHFU_RESERVE_BYTES':'1073741824'}),contextlib.redirect_stdout(io.StringIO()):
                search.build(db,[str(root)],images=False)
            c=search.connect(db,readonly=True)
            try:self.assertEqual(c.execute('SELECT embedding FROM chunks').fetchone()[0],blob)
            finally:c.close()
