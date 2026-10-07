import tempfile, unittest, os
from pathlib import Path
from unittest.mock import patch
import numpy as np
import search, retrieval

class ScopedVectorsTests(unittest.TestCase):
    def test_scoped_full_precision_skips_unrelated_blobs_and_preserves_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'index.db';c=search.connect(db)
            try:
                c.execute("INSERT INTO files(id,path,root,kind) VALUES(1,'/other/item','/other','text'),(2,'/selected/item','/selected','text')")
                blob=search.pack([1.0]+[0.0]*383)
                c.executemany('INSERT INTO chunks VALUES(?,1,0,?,?)',((i,'synthetic unrelated',blob) for i in range(1,1001)))
                c.executemany('INSERT INTO chunks VALUES(?,2,0,?,?)',((i,'synthetic selected',blob) for i in range(1001,1011)))
                c.commit();where=['f.path LIKE ?'];params=['/selected/%']
                with patch.object(retrieval,'_metadata',return_value={'total_kept':1010}):
                    self.assertTrue(retrieval._prefer_scoped_vectors(c,Path(tmp),where,params))
                    self.assertFalse(retrieval._prefer_scoped_vectors(c,Path(tmp),['f.path LIKE ?'],['/%']))
                q=np.array([[1.0]+[0.0]*383],dtype=np.float32)
                ann=Path(tmp)/'ann';ann.mkdir();(ann/'ann.meta.json').write_text('{}')
                class Model:
                    def text_vecs(self,texts):return np.repeat(q,len(texts),axis=0)
                with patch.dict(os.environ,{'SEARCHFU_ANN_DIR':str(ann)}),patch.object(retrieval,'_metadata',return_value={'total_kept':1010}),patch.object(retrieval,'_matrix_pass',side_effect=AssertionError('unrelated matrix scoring')):
                    events=list(retrieval.retrieve(db,'synthetic',path='/selected',models=Model()))
                self.assertTrue(events[-1]['complete'])
                self.assertEqual(events[-1]['coverage'],'full_precision_indexed_vectors')
                self.assertEqual(len(events[-1]['results']),1)
                with patch('os.walk',side_effect=AssertionError('source crawl')):
                    passes=list(retrieval._full_pass(c,q,10,where,params,lambda:None,8192,lower=1005))
                best,progress=passes[-1]
                self.assertEqual(progress['sqlite_vectors_scored'],5)
                self.assertEqual(best[0]['/selected/item'][0],1006)
                self.assertEqual(best[0]['/selected/item'][-1],1.0)
            finally:c.close()
