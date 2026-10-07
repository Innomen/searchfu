"""Copy and migrate a legacy Searchfu index without traversing its sources.

The source is read-only. Content and paths never appear in CLI diagnostics.
Use a new target, then build derived ANN caches from its stored vectors.
"""
from pathlib import Path
import argparse,json,os,sqlite3,tempfile
import search


def import_index(source,target):
    source=Path(source).resolve();target=Path(target).absolute()
    if target.exists():raise ValueError('target_already_exists')
    target.parent.mkdir(parents=True,exist_ok=True)
    src=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
    temporary=None
    try:
        required={'files':{'id','path','root','kind','mime','size','mtime','content_sig','indexed_at','text_embedding','image_embedding'},'chunks':{'id','file_id','ordinal','text','embedding'}}
        for table,columns in required.items():
            actual={r[1] for r in src.execute('PRAGMA table_info('+table+')')}
            if not columns<=actual:raise ValueError('unsupported_index_schema')
        if src.execute('SELECT count(*) FROM chunks WHERE embedding IS NOT NULL AND length(embedding)<>1536').fetchone()[0]:raise ValueError('unsupported_vector_dimension')
        fd,name=tempfile.mkstemp(prefix='.searchfu-import-',suffix='.sqlite3',dir=target.parent);os.close(fd);temporary=Path(name)
        dst=sqlite3.connect(temporary)
        try:src.backup(dst)
        finally:dst.close()
        c=search.connect(temporary)
        try:
            counts={'files_imported':c.execute('SELECT count(*) FROM files').fetchone()[0],'chunks_imported':c.execute('SELECT count(*) FROM chunks').fetchone()[0]}
            # Stat only stored paths to populate device filters; never enumerate.
            for fid,path in c.execute('SELECT id,path FROM files WHERE source_device IS NULL').fetchall():
                try:device=Path(path).stat().st_dev
                except OSError:continue
                c.execute('UPDATE files SET source_device=? WHERE id=?',(device,fid))
            c.commit();c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        finally:c.close()
        # No replacement, including a target created concurrently.
        os.link(temporary,target)
        return {**counts,'source_tree_walked':False,'reembedded':False,'source_modified':False}
    finally:
        src.close()
        if temporary:
            for path in (temporary,Path(str(temporary)+'-wal'),Path(str(temporary)+'-shm')):
                path.unlink(missing_ok=True)

if __name__=='__main__':
    os.umask(0o077)
    parser=argparse.ArgumentParser();parser.add_argument('source',type=Path);parser.add_argument('target',type=Path);args=parser.parse_args()
    try:print(json.dumps(import_index(args.source,args.target)))
    except Exception as exc:
        print(json.dumps({'ok':False,'error':'index_import_failed','exception_class':type(exc).__name__}));raise SystemExit(1)
