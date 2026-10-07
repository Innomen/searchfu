"""Bounded, opt-in read repair of cataloged plain text; no directory discovery."""
from pathlib import Path
from contextlib import closing, nullcontext
import contextlib, io
import fcntl, hashlib, json, os, signal, stat, subprocess, sys, time

LIMIT = 12
MAX_BYTES = 65536

def safe_read(path):
    # Open every component without following symlinks, including parent directories.
    parts=Path(path).parts
    if not Path(path).is_absolute() or '..' in parts: raise OSError('unsafe_path')
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for component in parts[1:-1]:
            new=os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=new
        leaf=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        with os.fdopen(leaf,'rb') as source:
            before=os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size>MAX_BYTES: raise OSError('ineligible_file')
            raw=source.read(MAX_BYTES+1)
            after=os.fstat(source.fileno())
            if len(raw)>MAX_BYTES or identity(before)!=identity(after): raise OSError('unstable_file')
            return raw,after
    finally: os.close(fd)

def identity(st): return (st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns)

def repair(request, *, encoder=None, admitted=None, cancelled=lambda:False, sample=True):
    from search import connect, chunks, file_sig, pack, TEXT_EXTS, SKIP_NAMES, Models
    from retrieval import filters, _extra
    from collections_config import scope_sql, load, config_path
    from storage import require_capacity, batch_bytes
    from gpu_lease import lease
    report={'checked':0,'updated':0,'skipped':0,'status':'complete'}
    with open(str(request['db'])+'.build.lock','a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return {**report,'status':'writer_busy'}
        with closing(connect(Path(request['db']))) as c:
            c.set_progress_handler(lambda:1 if cancelled() else 0,1000)
            opts=request['options']
            where,params=filters(opts.get('kind','all'),opts.get('after'),opts.get('before'),opts.get('path'))
            sw,sp,selected=scope_sql(opts.get('scopes',()),opts.get('collections_file'))
            where+=sw;params+=sp
            boundaries=load(opts.get("collections_file"))["collections"]
            sql='SELECT f.id,f.path,f.content_sig,f.source_device FROM files f WHERE f.kind="text"'+_extra(where)
            key='refresh_cursor_'+hashlib.sha256(json.dumps(opts,sort_keys=True).encode()).hexdigest()
            cursor=c.execute('SELECT value FROM index_state WHERE key=?',(key,)).fetchone()
            cursor=cursor[0] if cursor else 0
            sampled=list(c.execute(sql+' AND f.id>? ORDER BY f.id LIMIT ?',params+[cursor,LIMIT//2])) if sample else []
            if sample and not sampled: sampled=list(c.execute(sql+' ORDER BY f.id LIMIT ?',params+[LIMIT//2]))
            hits=[]
            for path in request.get('paths',[])[:LIMIT//2]:
                row=c.execute(sql+' AND f.path=?',params+[path]).fetchone()
                if row: hits.append(row)
            with c:
                if sampled:c.execute('INSERT OR REPLACE INTO index_state VALUES(?,?)',(key,sampled[-1][0]))
            pending=[]
            for fid,path,sig,device in dict((r[0],r) for r in hits+sampled).values():
                if cancelled():return {**report,'status':'cancelled'}
                try:
                    if any(part in SKIP_NAMES for part in Path(path).parts):raise OSError('excluded')
                    if Path(path).suffix.lower() not in TEXT_EXTS: raise OSError('unsupported')
                    if any(path==str(p) or path.startswith(str(p).rstrip('/')+'/') for p in (Path(request['db']).parent,Path(__file__).parent,Path.home()/'.cache',Path(opts.get("collections_file") or config_path()).parent,Path.home()/'.local/share/searchfu')): raise OSError('excluded')
                    for entry in boundaries.values():
                        root=entry['root'].rstrip('/')+'/'
                        if path==entry['root'] or path.startswith(root):
                            if entry.get('device') is not None and os.stat(entry['root']).st_dev!=entry['device']:raise OSError('mount_changed')
                            if any(path==p or path.startswith(p.rstrip('/')+'/') for p in entry.get('exclude',[])):raise OSError('excluded')
                    raw,st=safe_read(path);report['checked']+=1
                    if device is not None and st.st_dev!=device:raise OSError('device_changed')
                    if file_sig(st)==sig:continue
                    text=raw.decode('utf-8')
                    if '\x00' in text:raise ValueError('binary')
                    pending.append((fid,path,raw,st,chunks(text)))
                except (OSError,ValueError):report['skipped']+=1
            if cancelled():return {**report,'status':'cancelled'}
            if not pending:return report
            try:
                require_capacity(Path(request["db"]).parent,sum(batch_bytes(p[4]) for p in pending),phase="search_refresh")
                with ((admitted or lease)() if any(p[4] for p in pending) else nullcontext(lambda:None)) as lease_check:
                    encode=encoder or Models(False).text_vecs
                    for fid,path,raw,st,texts in pending:
                        if cancelled():return {**report,'status':'cancelled'}
                        lease_check();vectors=encode(texts) if texts else []
                        import numpy as np
                        if texts and (np.asarray(vectors).shape!=(len(texts),384) or not np.isfinite(vectors).all()):raise ValueError('invalid_vectors')
                        lease_check()
                        if cancelled():return {**report,'status':'cancelled'}
                        fresh,current=safe_read(path)
                        if identity(current)!=identity(st) or fresh!=raw:report['skipped']+=1;continue
                        high=0
                        ann=Path(os.environ.get('SEARCHFU_ANN_DIR',str(Path(request['db']).parent/'ann')))
                        for name,k in [('ann.meta.json','db_max_rowid'),('ann.progress.json','flushed_upto')]:
                            try:high=max(high,int(json.loads((ann/name).read_text()).get(k,0)))
                            except (OSError,ValueError,TypeError):pass
                        require_capacity(Path(request["db"]).parent,batch_bytes(texts),phase="search_refresh_commit")
                        with c:
                            high=max(high,c.execute("SELECT value FROM index_state WHERE key='chunk_high_water'").fetchone()[0])
                            c.execute('DELETE FROM chunks WHERE file_id=?',(fid,))
                            for i,(text,vec) in enumerate(zip(texts,vectors)):
                                c.execute('INSERT INTO chunks(id,file_id,ordinal,text,embedding) VALUES(?,?,?,?,?)',(high+i+1,fid,i,text,pack(vec)))
                            c.execute('DELETE FROM build_progress WHERE file_id=?',(fid,))
                            c.execute('UPDATE files SET size=?,mtime=?,content_sig=?,indexed_at=?,content_hash=?,extractor=?,text_embedding=NULL WHERE id=?',(st.st_size,st.st_mtime,file_sig(st),time.time(),hashlib.sha256(raw).hexdigest(),'plain-text-v1',fid))
                        report['updated']+=1
            except Exception as exc:
                report['status']={
                    'gpu_lease_unavailable':'gpu_unavailable',
                    'gpu_lease_lost':'gpu_unavailable',
                    'gpu_lease_release_unconfirmed':'lease_cleanup_failed',
                    'insufficient_capacity':'storage_unavailable',
                }.get(str(exc),'refresh_failed')
    return report

def run(db,paths,options,cancelled):
    env=os.environ.copy();env.update(SEARCHFU_CUDA='1',CUDA_VISIBLE_DEVICES='0',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1')
    request=json.dumps({'db':str(db),'paths':paths,'options':options})
    process=subprocess.Popen([sys.executable,str(Path(__file__).absolute())],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,env=env)
    sent=False
    try:
        while True:
            if cancelled() and not sent:process.send_signal(signal.SIGTERM);sent=True
            try:
                stdout,_=process.communicate(request,timeout=.1)
                break
            except subprocess.TimeoutExpired:request=None
        try:
            value=json.loads(stdout)
            if set(value)=={'checked','updated','skipped','status'} and all(type(value[k]) is int and 0<=value[k]<=LIMIT for k in ('checked','updated','skipped')) and value['status'] in ('complete','writer_busy','cancelled','refresh_failed','gpu_unavailable','lease_cleanup_failed','storage_unavailable'):return value
        except (ValueError,TypeError):pass
        return {'checked':0,'updated':0,'skipped':0,'status':'refresh_failed'}
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM);process.wait()

if __name__=='__main__':
    stopped=[False]
    signal.signal(signal.SIGTERM,lambda *_:stopped.__setitem__(0,True))
    signal.signal(signal.SIGINT,lambda *_:stopped.__setitem__(0,True))
    try:
        request=json.load(sys.stdin)
        with contextlib.redirect_stdout(io.StringIO()):
            result=repair(request,cancelled=lambda:stopped[0],sample=request.get('sample',True))
    except Exception:result={'checked':0,'updated':0,'skipped':0,'status':'refresh_failed'}
    print(json.dumps(result))
