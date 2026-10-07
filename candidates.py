"""Optional early semantic discovery, built only from indexed embeddings.

HNSW is deliberately opt-in and capped: it retains full vectors in RAM.
The canonical SQLite index and the later exhaustive pass remain authoritative.
"""
from contextlib import closing
from pathlib import Path
import argparse
import json
import os
import re
import uuid
import corpus_filter


def build(db, directory, *, max_rows=250000, threads=None):
    import numpy as np
    import hnswlib
    import fcntl
    from search import connect
    if max_rows<1: raise ValueError('invalid_candidate_limit')
    threads=threads or min(8,os.cpu_count() or 1)
    if threads<1: raise ValueError('invalid_threads')
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (directory/'.build.lock').open('a') as lock, closing(connect(Path(db),readonly=True)) as c:
        fcntl.flock(lock,fcntl.LOCK_EX)
        c.execute('BEGIN')
        count=c.execute('SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL').fetchone()[0]
        if count>max_rows: raise ValueError('candidate_row_limit_exceeded')
        identity=c.execute("SELECT value FROM index_state WHERE key='index_id'").fetchone()
        if not identity: raise ValueError('index_identity_required')
        old=None;pointer=directory/'candidates.meta.json'
        if pointer.exists(): old=json.loads(pointer.read_text()).get('file')
        index=hnswlib.Index(space='cosine',dim=384)
        index.init_index(max_elements=max(1,count),ef_construction=100,M=16,random_seed=42)
        index.set_num_threads(threads)
        cursor=c.execute('SELECT c.id,c.embedding,f.path FROM chunks c JOIN files f ON f.id=c.file_id ORDER BY c.id')
        kept=0
        while True:
            rows=cursor.fetchmany(4096)
            if not rows: break
            valid=[r for r in rows if r[1] is not None and len(r[1])==1536 and not corpus_filter.is_junk(r[2])]
            if valid:
                vectors=np.stack([np.frombuffer(r[1],dtype='<f4') for r in valid])
                if not np.isfinite(vectors).all(): raise ValueError('invalid_stored_vectors')
                index.add_items(vectors,np.asarray([r[0] for r in valid],dtype=np.int64));kept+=len(valid)
        name='graph-'+uuid.uuid4().hex+'.bin';target=directory/name
        try:
            index.save_index(str(target));target.chmod(0o600)
            meta={'format_version':1,'index_id':str(identity[0]),'filter_version':corpus_filter.FILTER_VERSION,
                  'dim':384,'rows':kept,'file':name,'db_max_rowid':c.execute('SELECT COALESCE(MAX(id),0) FROM chunks').fetchone()[0]}
            temp=pointer.with_suffix('.tmp');temp.write_text(json.dumps(meta));temp.chmod(0o600);temp.replace(pointer)
        except Exception:
            target.unlink(missing_ok=True);raise
        if old and re.fullmatch(r'graph-[0-9a-f]{32}\.bin',old) and old!=name:
            (directory/old).unlink(missing_ok=True)
        return {'built':True,'rows':kept,'bytes':target.stat().st_size,'threads':threads,'source_tree_walked':False}


def discover(c, directory, queries, limit, where, params, check, *, ef=500):
    """Bounded approximate pool; eligible candidates may be fewer than requested.

    Filters and live IDs are checked in SQLite. A sparse scope may underfill;
    later exhaustive stages resolve it without pretending this tier is complete.
    """
    from retrieval import _matrix_lock,_select
    import numpy as np
    directory=Path(directory)
    pointer=directory/'candidates.meta.json'
    if not pointer.is_file(): return None,{'reason':'candidate_cache_missing'}
    try: import hnswlib
    except ImportError: return None,{'reason':'candidate_dependency_missing'}
    with _matrix_lock(directory,check):
        if not pointer.is_file(): return None,{'reason':'candidate_cache_missing'}
        meta=json.loads(pointer.read_text())
        identity=c.execute("SELECT value FROM index_state WHERE key='index_id'").fetchone()
        if (meta.get('format_version')!=1 or meta.get('dim')!=384 or
            meta.get('filter_version')!=corpus_filter.FILTER_VERSION or not identity or
            meta.get('index_id')!=str(identity[0])):
            return None,{'reason':'candidate_cache_incompatible'}
        if not re.fullmatch(r'graph-[0-9a-f]{32}\.bin',str(meta.get('file',''))):
            return None,{'reason':'candidate_cache_invalid'}
        if not meta['rows']: return [{} for _ in queries],{'candidate_rows':0,'candidate_pool':0}
        check();index=hnswlib.Index(space='cosine',dim=384)
        index.load_index(str(directory/meta['file']));index.set_num_threads(min(4,os.cpu_count() or 1))
        k=min(meta['rows'],max(500,limit*4));index.set_ef(max(ef,k))
        check();labels,distances=index.knn_query(queries,k=k);check()
        best=[_select(c,ids,1-dist,limit,where,params) for ids,dist in zip(labels,distances)]
        return best,{'candidate_rows':meta['rows'],'candidate_pool':k,'ef':max(ef,k),'approximate':True}


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db',type=Path,required=True);ap.add_argument('--dir',type=Path)
    ap.add_argument('--max-rows',type=int,default=250000);ap.add_argument('--threads',type=int)
    a=ap.parse_args()
    try: print(json.dumps(build(a.db,a.dir or a.db.parent/'candidates',max_rows=a.max_rows,threads=a.threads)))
    except Exception as exc:
        print(json.dumps({'built':False,'error_class':type(exc).__name__,'source_tree_walked':False}));raise SystemExit(1)

if __name__=='__main__': main()
