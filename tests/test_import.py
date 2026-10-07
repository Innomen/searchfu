import contextlib,io,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import search,rag_import
from retrieval import retrieve

class ImportTests(unittest.TestCase):
    def test_import_uses_stored_vectors_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);source=base/'rag';source.mkdir();root=base/'corpus';root.mkdir();(root/'test.md').write_text('Synthetic fixture only.')
            (source/'index.json').write_text(json.dumps({'model':'all-MiniLM-L6-v2','embedding_dim':384}))
            (source/'chunks.json').write_text(json.dumps([{'file':'test.md','chunk_id':0}]))
            (source/'texts.json').write_text(json.dumps(['Synthetic stored passage about cancellation.']))
            np.save(source/'embeddings.npy',np.eye(1,384,dtype=np.float32));db=base/'index.sqlite3'
            with patch('os.walk',side_effect=AssertionError('walk')),patch.object(search.Models,'text_vecs',side_effect=AssertionError('embed')):
                self.assertEqual(rag_import.import_index(source,root,db)['chunks_imported'],1)
                self.assertEqual(rag_import.import_index(source,root,db)['chunks_imported'],0)
            results=list(retrieve(db,'cancellation',fts_only=True))[-1]['results']
            self.assertEqual(len(results),1)
            with patch.dict(os.environ,{'SEARCHFU_CUDA':'0','SEARCHFU_RESERVE_BYTES':str(1024**3)}),contextlib.redirect_stdout(io.StringIO()):
                search.build(db,[str(root)],images=False,names_only=True)
            self.assertEqual(len(list(retrieve(db,'cancellation',fts_only=True))[-1]['results']),1)
            c=search.connect(db,readonly=True)
            try:self.assertEqual(c.execute('SELECT source_device FROM files').fetchone()[0],root.stat().st_dev)
            finally:c.close()
            (source/'chunks.json').write_text(json.dumps([{'file':'../escape.md'}]))
            with self.assertRaises(ValueError):rag_import.import_index(source,root,db)
    @unittest.skipUnless(hasattr(os,'mkfifo'),'POSIX only')
    def test_index_skips_fifo_without_reading_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'corpus';root.mkdir();os.mkfifo(root/'hang.txt')
            with patch.dict(os.environ,{'SEARCHFU_CUDA':'0','SEARCHFU_RESERVE_BYTES':str(1024**3)}),contextlib.redirect_stdout(io.StringIO()):
                search.build(Path(tmp)/'index.sqlite3',[str(root)],images=False)
            c=search.connect(Path(tmp)/'index.sqlite3',readonly=True)
            try:self.assertEqual(c.execute('SELECT count(*) FROM files').fetchone()[0],0)
            finally:c.close()

    def test_failed_extraction_keeps_filename_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'corpus';root.mkdir();(root/'unreadable.docx').write_bytes(b'synthetic invalid zip')
            with patch.dict(os.environ,{'SEARCHFU_CUDA':'0','SEARCHFU_RESERVE_BYTES':str(1024**3)}),contextlib.redirect_stdout(io.StringIO()):
                search.build(Path(tmp)/'index.sqlite3',[str(root)],images=False)
            rows=list(retrieve(Path(tmp)/'index.sqlite3','unreadable.docx',names_only=True))[-1]['results']
            self.assertEqual(len(rows),1);self.assertIsNone(rows[0]['snippet'])
