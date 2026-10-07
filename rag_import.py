"""Explicit import of stored MiniLM RAG data; never traverses source files.

The importer is a local content operation. Remote advisers must only receive
its aggregate return value, never the input JSON, vectors or imported rows.
"""
from pathlib import Path
import json,hashlib,time
import numpy as np
import search


def import_index(source,root,db):
    source=Path(source);root=Path(root).absolute();db=Path(db)
    meta=json.loads((source/'index.json').read_text())
    if meta.get('model')!='all-MiniLM-L6-v2' or meta.get('embedding_dim')!=384:raise ValueError('unsupported_rag_model')
    vectors=np.load(source/'embeddings.npy',mmap_mode='r',allow_pickle=False)
    rows=json.loads((source/'chunks.json').read_text());texts=json.loads((source/'texts.json').read_text())
    if vectors.shape!=(len(rows),384) or len(rows)!=len(texts):raise ValueError('rag_alignment_error')
    # Validate all inputs before touching canonical state; avoid unsafe pickle state.
    groups={}
    for i,(row,text) in enumerate(zip(rows,texts)):
        rel=Path(row['file'])
        if rel.is_absolute() or '..' in rel.parts or not isinstance(text,str):raise ValueError('invalid_rag_row')
        if not np.isfinite(vectors[i]).all():raise ValueError('invalid_rag_vector')
        groups.setdefault(str(root/rel),[]).append((int(row.get('chunk_id',len(groups))),text,vectors[i]))
    # Explicit import may stat known metadata paths, but never enumerates the tree.
    devices={}
    for path in groups:
        try:devices[path]=Path(path).stat().st_dev
        except OSError:devices[path]=None
    c=search.connect(db);inserted=0
    try:
        with c:
            high=c.execute("SELECT value FROM index_state WHERE key='chunk_high_water'").fetchone()[0]
            for path,chunks in groups.items():
                existing=c.execute('SELECT id FROM files WHERE path=?',(path,)).fetchone()
                if existing:continue # Idempotent bootstrap; never overwrite newer indexed files.
                fid=c.execute('INSERT INTO files(path,root,kind,mime,size,mtime,extractor,source_device) VALUES(?,?,?,?,?,?,?,?)',(path,str(root),'text','text/markdown',0,0,'rag-import-v1',devices[path])).lastrowid
                for ordinal,text,vec in chunks:
                    high+=1;c.execute('INSERT INTO chunks(id,file_id,ordinal,text,embedding) VALUES(?,?,?,?,?)',(high,fid,ordinal,text,search.pack(vec)));inserted+=1
            c.execute("INSERT OR REPLACE INTO index_state VALUES('last_import_finished',?)",(time.time(),))
        return {'files_available':len(groups),'chunks_imported':inserted,'source_tree_walked':False}
    finally:c.close()
