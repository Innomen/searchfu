"""Local shadow searches. Only fixed numeric aggregates cross the metrics boundary.

Requests and result identities travel through anonymous pipes, never files or logs.
The detached supervisor owns a read-only shadow through timeout/exit even when a
short-lived CLI finishes. Primary retrieval never waits for the shadow.
"""
from pathlib import Path
import json, math, os, sqlite3, subprocess, sys, threading, time
from contextlib import closing

BUCKETS=(.05,.1,.25,.5,1,2,5,10,30,60,120,300)
MODES=('names','keyword','hybrid','deep')
OUTCOMES=('paired','missing_target','disabled','busy','unsupported','shadow_failed','shadow_timeout','primary_failed','cancelled','same_target')

def outside_source(path):
    p=Path(path)
    if p.resolve().is_relative_to(Path(__file__).resolve().parent):raise ValueError('runtime_data_must_live_outside_source')
    return p

def config_path():
    return outside_source(os.environ.get('SEARCHFU_COMPARISON_CONFIG',str(Path(os.environ.get('XDG_CONFIG_HOME',str(Path.home()/'.config')))/'searchfu/comparison.json')))

def metrics_path():
    return outside_source(os.environ.get('SEARCHFU_METRICS_DB',str(Path(os.environ.get('XDG_STATE_HOME',str(Path.home()/'.local/state')))/'searchfu/metrics.sqlite3')))

def load():
    p=config_path()
    data=json.loads(p.read_text()) if p.exists() else {}
    return validate({'enabled':True,'usb_db':'','usb_ann':'','usb_candidates':'','timeout_seconds':120,**data})

def validate(data):
    if set(data)!={'enabled','usb_db','usb_ann','usb_candidates','timeout_seconds'}:raise ValueError('invalid_comparison_settings')
    if type(data['enabled']) is not bool:raise ValueError('invalid_comparison_settings')
    for key in ('usb_db','usb_ann','usb_candidates'):
        if not isinstance(data[key],str) or (data[key] and not Path(data[key]).is_absolute()):raise ValueError('invalid_comparison_settings')
    n=data['timeout_seconds']
    if type(n) not in (int,float) or not math.isfinite(n) or not 1<=n<=300:raise ValueError('invalid_comparison_settings')
    return data

def save(data):
    import tempfile
    data=validate(data);p=config_path();p.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,name=tempfile.mkstemp(dir=p.parent,prefix='.comparison-')
    try:
        with os.fdopen(fd,'w') as out:json.dump(data,out)
        os.replace(name,p)
    finally:
        if os.path.exists(name):os.unlink(name)

def _connection():
    p=metrics_path();
    if p.is_symlink():raise ValueError('invalid_metrics_path')
    p.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(p,os.O_CREAT|os.O_WRONLY,0o600);os.close(fd)
    c=sqlite3.connect(p,timeout=10)
    c.execute('CREATE TABLE IF NOT EXISTS counters(mode TEXT, metric TEXT, value REAL NOT NULL, PRIMARY KEY(mode,metric))')
    return c

def record(mode,outcome,primary=None,shadow=None):
    """Accept no free-form text. Never store per-search rows or timestamps."""
    if mode not in MODES or outcome not in OUTCOMES:raise ValueError('invalid_metric_category')
    values={outcome:1}
    if outcome=='paired':
        for label,s in (('nvme',primary),('usb',shadow)):
            for name in ('seconds','first_seconds','count','index_seconds','encoding_seconds'):
                n=s.get(name,0)
                if type(n) not in (int,float) or not math.isfinite(n) or not 0<=n<=1e9:raise ValueError('invalid_metric_value')
                values[label+'_'+name+'_sum']=n
            bucket=next((i for i,b in enumerate(BUCKETS) if s['seconds']<=b),len(BUCKETS))
            values[label+'_bucket_'+str(bucket)]=1
        a,b=set(primary['ids']),set(shadow['ids'])
        # Identities are compared only in memory. No hashes survive this function.
        values['overlap_sum']=len(a&b)/max(1,len(a|b)) if a or b else 1
        values['same_first']=int(bool(primary['ids'] and shadow['ids'] and primary['ids'][0]==shadow['ids'][0]))
        values['usb_faster']=int(shadow['seconds']<primary['seconds'])
        values['nvme_refresh_enabled']=int(primary.get('refresh',False))
    with closing(_connection()) as c,c:
        for key,value in values.items():
            c.execute('INSERT INTO counters VALUES(?,?,?) ON CONFLICT(mode,metric) DO UPDATE SET value=value+excluded.value',(mode,key,value))

def report():
    if not metrics_path().exists():return {'version':1,'modes':{},'measurement':'concurrent_read_only_shadow'}
    with closing(_connection()) as c:
        out={}
        for mode,key,value in c.execute('SELECT mode,metric,value FROM counters'):
            # Deliberately closed export vocabulary: unexpected stored data stays local.
            allowed=set(OUTCOMES)|{'overlap_sum','same_first','usb_faster','nvme_refresh_enabled'}|{f'{s}_{n}_sum' for s in ('nvme','usb') for n in ('seconds','first_seconds','count','index_seconds','encoding_seconds')}|{f'{s}_bucket_{i}' for s in ('nvme','usb') for i in range(len(BUCKETS)+1)}
            if mode not in MODES or key not in allowed or type(value) not in (int,float) or not math.isfinite(value) or value<0:continue
            out.setdefault(mode,{})[key]=value
    return {'version':1,'modes':out,'latency_bucket_upper_seconds':list(BUCKETS),'measurement':'concurrent_read_only_shadow'}

def reset():
    with closing(_connection()) as c,c:c.execute('DELETE FROM counters')

def footprint(db,ann=None,candidates=None):
    """Explicit index artifacts only; never traverse source roots or follow symlinks."""
    def size(p):
        try:return p.stat().st_size
        except OSError:return 0
    def allocated(p):
        try:return p.stat().st_blocks*512
        except OSError:return 0
    db=Path(db)
    out={'available':db.is_file(),'database_bytes':size(db),'journal_bytes':sum(size(Path(str(db)+s)) for s in ('-wal','-shm')),'cache_bytes':0,'database_allocated_bytes':allocated(db),'journal_allocated_bytes':sum(allocated(Path(str(db)+s)) for s in ('-wal','-shm')),'cache_allocated_bytes':0}
    for root in (ann,candidates):
        if not root:continue
        for parent,dirs,files in os.walk(root,followlinks=False):
            dirs[:]=[d for d in dirs if not Path(parent,d).is_symlink()]
            paths=[Path(parent,n) for n in files if not Path(parent,n).is_symlink()]
            out['cache_bytes']+=sum(size(p) for p in paths)
            out['cache_allocated_bytes']+=sum(allocated(p) for p in paths)
    return out

def mode(options):
    return 'names' if options.get('names_only') else 'keyword' if options.get('fts_only') else 'deep' if options.get('deep') else 'hybrid'

def summary(event,first,refresh=False):
    return {'seconds':event.get('elapsed_seconds',0),'first_seconds':first or 0,'count':len(event.get('results',[])), 'ids':[r['result_id'] for r in event.get('results',[])], 'refresh':refresh,'index_seconds':event.get('timings',{}).get('index_seconds',event.get('elapsed_seconds',0)),'encoding_seconds':event.get('timings',{}).get('encoding_seconds',0)}

def launch(db,query,options):
    m=mode(options)
    try:cfg=load()
    except Exception:return None  # Broken optional configuration must not break search.
    if not cfg['enabled']:return None
    if not cfg['usb_db'] or not Path(cfg['usb_db']).is_file():record(m,'missing_target');return None
    if Path(cfg['usb_db']).resolve()==Path(db).resolve():record(m,'same_target');return None
    if options.get('reranker') is not None:record(m,'unsupported');return None
    safe={k:v for k,v in options.items() if k not in ('models','reranker','cancelled')}
    payload=json.dumps({'config':cfg,'query':query,'options':safe,'mode':m})+'\n'
    env={**os.environ,'SEARCHFU_SHADOW':'1','SEARCHFU_CUDA':'0','CUDA_VISIBLE_DEVICES':''}
    p=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--supervisor'],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,text=True,env=env,start_new_session=True)
    try:p.stdin.write(payload);p.stdin.flush()
    except Exception:
        p.stdin.close();p.terminate();p.wait();raise
    threading.Thread(target=p.wait,daemon=True).start()
    return p

def finish(p,outcome,primary=None):
    if p is None:return
    try:p.stdin.write(json.dumps({'outcome':outcome,'primary':primary})+'\n');p.stdin.close()
    except (BrokenPipeError,OSError):pass

def supervisor():
    import fcntl
    request=json.loads(sys.stdin.readline());cfg=request['config'];m=request['mode']
    # One shadow at a time across CLI/EMS/GUI processes, no backlog of private queries.
    lockpath=metrics_path().parent/'comparison.lock';lockpath.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(lockpath,os.O_CREAT|os.O_RDWR,0o600)
    with os.fdopen(fd,'w') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:record(m,'busy');return
        env={**os.environ,'SEARCHFU_ANN_DIR':cfg['usb_ann'] or str(Path(cfg['usb_db']).parent/'ann'),'SEARCHFU_CANDIDATES_DIR':cfg['usb_candidates'] or str(Path(cfg['usb_db']).parent/'candidates')}
        child=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--shadow'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,env=env)
        answer={}
        def collect():
            try:
                raw,_=child.communicate(json.dumps(request),timeout=cfg['timeout_seconds'])
                answer.update(json.loads(raw) if child.returncode==0 else {'outcome':'shadow_failed'})
            except subprocess.TimeoutExpired:
                child.kill();child.communicate();answer['outcome']='shadow_timeout'
            except Exception:
                child.kill();child.communicate();answer['outcome']='shadow_failed'
        t=threading.Thread(target=collect);t.start()
        try:
            line=sys.stdin.readline();primary=json.loads(line) if line else {'outcome':'cancelled'}
            if primary['outcome']!='paired' and child.poll() is None:child.kill()
            t.join()
            outcome=primary['outcome'] if primary['outcome']!='paired' else answer.get('outcome','shadow_failed')
            record(m,outcome,primary.get('primary'),answer.get('summary'))
        finally:
            if child.poll() is None:child.kill()
            t.join();child.wait()

def shadow():
    request=json.load(sys.stdin);cfg=request['config'];options=request['options']
    options.update(refresh=False,deltas=False,max_seconds=min(options.get('max_seconds') or cfg['timeout_seconds'],cfg['timeout_seconds']))
    from retrieval import retrieve
    first=None;last=None
    for e in retrieve(cfg['usb_db'],request['query'],comparison=False,**options):
        if first is None and 'results' in e:first=e['elapsed_seconds']
        last=e
    print(json.dumps({'outcome':'paired','summary':summary(last,first)} if last and last['stage']=='done' else {'outcome':'shadow_timeout'}))

if __name__=='__main__':
    try:
        if '--supervisor' in sys.argv:supervisor()
        elif '--shadow' in sys.argv:shadow()
        else:
            import argparse
            ap=argparse.ArgumentParser(description='Payload-free aggregate Searchfu metrics')
            ap.add_argument('--footprint',action='store_true')
            ap.add_argument('--reset',action='store_true')
            ap.add_argument('--set-usb-db',type=Path)
            ap.add_argument('--agent',action='store_true',help='closed aggregate schema, no content or paths (also the default)')
            args=ap.parse_args()
            if args.set_usb_db:
                cfg=load();cfg['usb_db']=str(args.set_usb_db.resolve());save(cfg)
            if args.reset:reset()
            out=report();cfg=load()
            out['configuration']={'enabled':cfg['enabled'],'usb_configured':bool(cfg['usb_db']),'usb_available':bool(cfg['usb_db'] and Path(cfg['usb_db']).is_file()),'timeout_seconds':cfg['timeout_seconds']}
            if args.footprint:
                from runtime_paths import pinned_paths
                p=pinned_paths();out['footprint']={'nvme':footprint(p['SEARCHFU_DB'],p['SEARCHFU_ANN_DIR'],p['SEARCHFU_CANDIDATES_DIR'])}
                if cfg['usb_db']:out['footprint']['usb']=footprint(cfg['usb_db'],cfg['usb_ann'] or str(Path(cfg['usb_db']).parent/'ann'),cfg['usb_candidates'] or str(Path(cfg['usb_db']).parent/'candidates'))
            print(json.dumps(out,indent=2))
    except Exception:sys.exit(1)  # No exception details or private request in logs.
