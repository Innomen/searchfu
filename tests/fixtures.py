"""Generated test data only; paths refer to a nonexistent source tree."""
from pathlib import Path
import json
import numpy as np
import search
from ann import quantize

class FakeModels:
    def __init__(self,v): self.v=np.asarray(v,dtype=np.float32);self.calls=0
    def text_vecs(self,texts):
        self.calls+=1
        return np.stack([self.v for _ in texts])


def fixture(root,rows=200,files=50,dtype="int8",tail=0,block_rows=128):
    rng=np.random.default_rng(42)
    vectors=rng.normal(size=(rows,384)).astype(np.float32)
    vectors/=np.linalg.norm(vectors,axis=1,keepdims=True)
    db=root/'drive.sqlite3';ann=root/'ann';blocks=ann/'blocks';blocks.mkdir(parents=True)
    c=search.connect(db)
    for i in range(files):
        c.execute('INSERT INTO files(id,path,root,kind,mime,size,mtime,indexed_at) VALUES(?,?,?,?,?,?,?,?)',
                  (i+1,f'/synthetic-corpus/docs/item-{i}.txt','/synthetic-corpus','text','text/plain',120,1,1))
    for i,v in enumerate(vectors):
        text=f'Topic{i%40} synthetic evidence for document {i%files}. Distinctive research tags and sample context.'
        c.execute('INSERT INTO chunks(id,file_id,ordinal,text,embedding) VALUES(?,?,?,?,?)',
                  (i+1,i%files+1,i//files,text,search.pack(v)))
    c.commit()
    identity=c.execute("SELECT value FROM index_state WHERE key='index_id'").fetchone()[0]
    n=rows-tail;scale=np.maximum(np.abs(vectors[:n]).max(axis=0)*1.1,1e-12)
    bi=0
    for lo in range(0,n,block_rows):
        hi=min(n,lo+block_rows)
        np.save(blocks/f'emb-{bi:04d}.npy',quantize(vectors[lo:hi],scale,dtype))
        np.save(blocks/f'ids-{bi:04d}.npy',np.arange(lo+1,hi+1,dtype=np.int64));bi+=1
    np.save(blocks/'ids.npy',np.arange(1,n+1,dtype=np.int64))
    (ann/'ann.meta.json').write_text(json.dumps({'format_version':2,'index_id':identity,
        'dtype':dtype,'dim':384,'scale':scale.tolist(),'total_kept':n,'n_blocks':bi,
        'db_max_rowid':n,'filter_version':1}))
    (ann/'.build.lock').touch()
    (ann/'ann.progress.json').write_text(json.dumps({'flushed_upto':n,'kept':n,'pruned':0,
        'skipped':0,'bi':bi,'dtype':dtype,'index_id':identity,'filter_version':1}))
    c.close()
    return db,ann,vectors
