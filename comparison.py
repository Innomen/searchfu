"""Local shadow searches. Only fixed numeric aggregates cross the metrics boundary.

Requests and result identities travel through anonymous pipes, never files or logs.
The detached supervisor owns a read-only shadow through timeout/exit even when a
short-lived CLI finishes. Primary retrieval never waits for the shadow.
"""
from pathlib import Path
import json, math, os, sqlite3, subprocess, sys, threading, time
import diagnostics
from contextlib import closing

BUCKETS=(.05,.1,.25,.5,1,2,5,10,30,60,120,300)
MODES=('names','keyword','hybrid','deep')
OUTCOMES=('paired','missing_target','disabled','busy','unsupported','legacy_scope_unsupported','shadow_failed','shadow_timeout','primary_failed','primary_timeout','cancelled','same_target')

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
    if type(n) not in (int,float) or not math.isfinite(n) or not 0<=n<=300:raise ValueError('invalid_comparison_settings')
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

def record(mode,outcome,primary=None,shadow=None,snapshot_matched=False):
    """Accept no free-form text. Never store per-search rows or timestamps."""
    if mode not in MODES or outcome not in OUTCOMES:raise ValueError('invalid_metric_category')
    values={outcome:1}
    if outcome=='paired':
        values['matched_snapshot_pairs' if snapshot_matched else 'unmatched_snapshot_pairs']=1
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

PRIMARY_KEYS={'primary_attempts','primary_completed','primary_cancelled','primary_timeouts','primary_failures','primary_seconds_sum','primary_first_seconds_sum','primary_count_sum'}
EXPERIMENT_KEYS={f'{role}_observed_{field}' for role in ('primary','shadow') for field in ('seconds','peak_results','events','first_seconds','read_bytes','cpu_seconds')}|{'shadow_attempts','shadow_completed','shadow_cancelled','shadow_timeouts','shadow_failures','shadow_final_count_sum','shadow_partial_count_sum'}

def bank(m,values):
    if m not in MODES or set(values)-EXPERIMENT_KEYS:raise ValueError('invalid_observation')
    for n in values.values():
        if type(n) not in (int,float) or not math.isfinite(n) or not 0<=n<=1e18:raise ValueError('invalid_observation')
    with closing(_connection()) as c,c:
        for key,n in values.items():c.execute('INSERT INTO counters VALUES(?,?,?) ON CONFLICT(mode,metric) DO UPDATE SET value=value+excluded.value',(m,key,n))

class Observations:
    """Bank monotonic deltas, never an identifiable per-search record."""
    def __init__(self,m,role):
        self.m=m;self.role=role;self.previous={};self.first=None;self.peak=0;self.events=0;self.lock=threading.Lock()
    def checkpoint(self,seconds,count=None,*,event=False,read_bytes=0,cpu_seconds=0):
        with self.lock:self._checkpoint(seconds,count,event=event,read_bytes=read_bytes,cpu_seconds=cpu_seconds)
    def _checkpoint(self,seconds,count=None,*,event=False,read_bytes=0,cpu_seconds=0):
        if count is not None:
            self.peak=max(self.peak,count)
            if count and self.first is None:self.first=seconds
        self.events+=int(event)
        current={'seconds':seconds,'peak_results':self.peak,'events':self.events,'first_seconds':self.first or 0,'read_bytes':read_bytes,'cpu_seconds':cpu_seconds}
        values={self.role+'_observed_'+key:max(0,n-self.previous.get(key,0)) for key,n in current.items()}
        bank(self.m,values);self.previous={key:max(n,self.previous.get(key,0)) for key,n in current.items()}

def record_primary(m,outcome,s):
    if m not in MODES or outcome not in ('paired','cancelled','primary_timeout','primary_failed'):raise ValueError('invalid_primary_metric')
    # Independent attempts must not share counters with pairing outcomes.
    counter={'paired':'primary_completed','cancelled':'primary_cancelled','primary_timeout':'primary_timeouts','primary_failed':'primary_failures'}[outcome]
    values={'primary_attempts':1,counter:1}
    for key,field in [('primary_seconds_sum','seconds'),('primary_first_seconds_sum','first_seconds'),('primary_count_sum','count')]:
        n=s.get(field,0)
        if type(n) not in (int,float) or not math.isfinite(n) or not 0<=n<=1e9:raise ValueError('invalid_primary_metric')
        values[key]=n
    with closing(_connection()) as c,c:
        for key,value in values.items():c.execute('INSERT INTO counters VALUES(?,?,?) ON CONFLICT(mode,metric) DO UPDATE SET value=value+excluded.value',(m,key,value))

def activity():
    """Probe only the developer-owned supervisor lock; never inspect job stores."""
    import fcntl
    p=metrics_path().parent/'comparison.lock'
    if not p.exists():return {'active':False}
    try:
        with p.open('r') as f:
            try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return {'active':True}
        return {'active':False}
    except OSError:return {'active':False,'unavailable':True}

def cancel_active():
    if not activity()['active']:return False
    fd=os.open(metrics_path().parent/'comparison.cancel',os.O_CREAT|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    os.close(fd);return True

def report():
    if not metrics_path().exists():return {'version':1,'modes':{},'measurement':'concurrent_read_only_shadow','activity':activity()}
    with closing(_connection()) as c:
        out={}
        for mode,key,value in c.execute('SELECT mode,metric,value FROM counters'):
            # Deliberately closed export vocabulary: unexpected stored data stays local.
            allowed=set(OUTCOMES)|PRIMARY_KEYS|EXPERIMENT_KEYS|{'overlap_sum','same_first','usb_faster','nvme_refresh_enabled','matched_snapshot_pairs','unmatched_snapshot_pairs'}|{f'{s}_{n}_sum' for s in ('nvme','usb') for n in ('seconds','first_seconds','count','index_seconds','encoding_seconds')}|{f'{s}_bucket_{i}' for s in ('nvme','usb') for i in range(len(BUCKETS)+1)}
            if mode not in MODES or key not in allowed or type(value) not in (int,float) or not math.isfinite(value) or value<0:continue
            out.setdefault(mode,{})[key]=value
    return {'version':1,'modes':out,'latency_bucket_upper_seconds':list(BUCKETS),'measurement':'concurrent_read_only_shadow','activity':activity()}

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

def scope_supported(db,options):
    """Schema-only admission: never interpret a missing device as zero hits."""
    names=options.get('scopes') or []
    if not names:return True
    import collections_config
    scopes=collections_config.load()['collections']
    if not any(scopes.get(name,{}).get('one_filesystem') for name in names):return True
    with closing(sqlite3.connect(Path(db).resolve().as_uri()+'?mode=ro',uri=True)) as c:
        if not any(row[1]=='source_device' for row in c.execute('PRAGMA main.table_info(files)')):return False
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='index_state' AND type='table'").fetchone() and c.execute("SELECT 1 FROM index_state WHERE key='metadata_device_upgrade'").fetchone():
            for name in names:
                entry=scopes.get(name,{})
                if not entry.get('one_filesystem'):continue
                root=entry['root'].rstrip('/') or '/';prefix=root.rstrip('/')+'/'
                if c.execute('SELECT 1 FROM files WHERE source_device IS NULL AND (path=? OR substr(path,1,?)=?) LIMIT 1',(root,len(prefix),prefix)).fetchone():return False
        return True

def launch(db,query,options):
    m=mode(options)
    try:cfg=load()
    except Exception as exc:diagnostics.record('comparison','invalid_configuration',exc);return None  # Optional configuration must not break search.
    if not cfg['enabled']:return None
    if not cfg['usb_db'] or not Path(cfg['usb_db']).is_file():diagnostics.record('comparison','missing_target');record(m,'missing_target');return None
    if Path(cfg['usb_db']).resolve()==Path(db).resolve():diagnostics.record('comparison','same_target');record(m,'same_target');return None
    if options.get('reranker') is not None:record(m,'unsupported');return None
    if not scope_supported(cfg['usb_db'],options):
        diagnostics.record('comparison','legacy_scope_unsupported');record(m,'legacy_scope_unsupported');return None
    safe={k:v for k,v in options.items() if k not in ('models','reranker','cancelled')}
    from replication import snapshot_matches
    payload=json.dumps({'config':cfg,'query':query,'options':safe,'mode':m,'snapshot_matched':snapshot_matches(cfg)})+'\n'
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
        cancelpath=metrics_path().parent/'comparison.cancel';cancelpath.unlink(missing_ok=True)
        env={**os.environ,'SEARCHFU_ANN_DIR':cfg['usb_ann'] or str(Path(cfg['usb_db']).parent/'ann'),'SEARCHFU_CANDIDATES_DIR':cfg['usb_candidates'] or str(Path(cfg['usb_db']).parent/'candidates')}
        child=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--shadow'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,env=env)
        answer={};observations=Observations(m,'shadow');bank(m,{'shadow_attempts':1});started=time.monotonic();timed_out=threading.Event();cancelled=threading.Event();stopping=threading.Event()
        def sample():
            io=cpu=0
            try:
                fields=Path('/proc',str(child.pid),'io').read_text().splitlines()
                io=next(int(line.split(':')[1]) for line in fields if line.startswith('read_bytes:'))
                fields=Path('/proc',str(child.pid),'stat').read_text().rsplit(')',1)[1].split()
                cpu=(int(fields[11])+int(fields[12]))/os.sysconf('SC_CLK_TCK')
            except (OSError,ValueError,StopIteration,IndexError):pass
            observations.checkpoint(time.monotonic()-started,read_bytes=io,cpu_seconds=cpu)
        def collect():
            try:
                child.stdin.write(json.dumps(request));child.stdin.close()
                for line in child.stdout:
                    response=json.loads(line)
                    if 'summary' in response:
                        s=response['summary'];answer['summary']=s
                        observations.checkpoint(s['seconds'],s['count'],event=True)
                    if 'outcome' in response:answer['outcome']=response['outcome']
                child.wait()
                if cancelled.is_set():answer['outcome']='cancelled'
                elif timed_out.is_set():answer['outcome']='shadow_timeout'
                elif child.returncode:answer['outcome']='shadow_failed'
            except Exception:
                if child.poll() is None:child.kill()
                child.wait();answer['outcome']='shadow_failed'
            finally:stopping.set()
        def monitor():
            while not stopping.wait(5):
                try:sample()
                except Exception as exc:diagnostics.record('comparison','operation_failed',exc)
                if cancelpath.exists():
                    cancelled.set()
                    if child.poll() is None:child.kill()
                    return
                if cfg['timeout_seconds'] and time.monotonic()-started>=cfg['timeout_seconds']:
                    timed_out.set()
                    if child.poll() is None:child.kill()
                    return
        # A separate exact timer retains bounded mode's existing hard guarantee.
        def expire():
            timed_out.set()
            if child.poll() is None:child.kill()
        timer=threading.Timer(cfg['timeout_seconds'],expire) if cfg['timeout_seconds'] else None
        if timer:timer.start()
        guard=threading.Thread(target=monitor);guard.start()
        t=threading.Thread(target=collect);t.start()
        try:
            line=sys.stdin.readline();primary=json.loads(line) if line else {'outcome':'cancelled'}
            if primary['outcome']!='paired' and child.poll() is None:child.kill()
            t.join()
            outcome=primary['outcome'] if primary['outcome']!='paired' else answer.get('outcome','shadow_failed')
            sample()
            result_count=answer.get('summary',{}).get('count',0)
            bank(m,{'shadow_completed' if outcome=='paired' else 'shadow_timeouts' if outcome=='shadow_timeout' else 'shadow_cancelled' if outcome in ('cancelled','primary_timeout') else 'shadow_failures':1,
                    'shadow_final_count_sum' if outcome=='paired' else 'shadow_partial_count_sum':result_count})
            if outcome in ('shadow_failed','shadow_timeout'):diagnostics.record('comparison',outcome)
            from replication import snapshot_matches
            matched=bool(request.get('snapshot_matched') and not primary.get('primary',{}).get('refresh',False) and snapshot_matches(cfg))
            record(m,outcome,primary.get('primary'),answer.get('summary'),snapshot_matched=matched)
        finally:
            if child.poll() is None:child.kill()
            t.join();child.wait();stopping.set();guard.join()
            if timer:timer.cancel();timer.join()
            cancelpath.unlink(missing_ok=True)

def shadow():
    request=json.load(sys.stdin);cfg=request['config'];options=request['options']
    budgets=[n for n in (options.get('max_seconds'),cfg['timeout_seconds']) if n]
    options.update(refresh=False,deltas=False,max_seconds=min(budgets) if budgets else None)
    from retrieval import retrieve
    from evidence_bank import EvidenceBank
    evidence=EvidenceBank('usb-comparison')
    first=None;last=None
    for e in retrieve(cfg['usb_db'],request['query'],comparison=False,**options):
        if first is None and e.get('results'):first=e['elapsed_seconds']
        last=e
        evidence.append(e)
        print(json.dumps({'summary':summary(e,first)}),flush=True)
    print(json.dumps({'outcome':'paired' if last and last['stage']=='done' else 'shadow_timeout','summary':summary(last,first) if last else {'seconds':0,'count':0,'ids':[]}}),flush=True)

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
    except Exception as exc:diagnostics.record('comparison','operation_failed',exc);sys.exit(1)  # No private exception details.
