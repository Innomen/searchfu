"""Payload-free error counters. Never accepts exception text, paths or tracebacks."""
from pathlib import Path
from contextlib import closing
import json, os, sqlite3

COMPONENTS={'gui','search_worker','comparison','catalog','settings'}
CODES={'operation_failed','worker_failed','invalid_configuration','missing_target','same_target','legacy_scope_unsupported','shadow_failed','shadow_timeout','database_failed','permission_denied','io_failed','cancelled'}
CLASSES={'none','sqlite','permission','os','value','other'}

def log_path():
    default=Path(os.environ.get('XDG_STATE_HOME',str(Path.home()/'.local/state')))/'searchfu/errors.sqlite3'
    # Synthetic metric overrides also isolate diagnostic storage.
    if os.environ.get('SEARCHFU_METRICS_DB'):default=Path(os.environ['SEARCHFU_METRICS_DB']).parent/'errors.sqlite3'
    p=Path(os.environ.get('SEARCHFU_ERRORS_DB',str(default)))
    if p.resolve().is_relative_to(Path(__file__).resolve().parent) or p.is_symlink():raise ValueError('invalid_diagnostic_location')
    return p

def category(exc):
    if exc is None:return 'none'
    if isinstance(exc,sqlite3.Error):return 'sqlite'
    if isinstance(exc,PermissionError):return 'permission'
    if isinstance(exc,OSError):return 'os'
    if isinstance(exc,ValueError):return 'value'
    return 'other'

def record(component,code,exc=None):
    if component not in COMPONENTS or code not in CODES:raise ValueError('invalid_diagnostic_category')
    # Logging failure must never replace the application error or print details.
    try:
        p=log_path();p.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd=os.open(p,os.O_CREAT|os.O_WRONLY|os.O_NOFOLLOW,0o600);os.fchmod(fd,0o600);os.close(fd)
        with closing(sqlite3.connect(p,timeout=1)) as c,c:
            c.execute('CREATE TABLE IF NOT EXISTS errors(component TEXT, code TEXT, category TEXT, count INTEGER, PRIMARY KEY(component,code,category))')
            c.execute('INSERT INTO errors VALUES(?,?,?,1) ON CONFLICT(component,code,category) DO UPDATE SET count=count+1',(component,code,category(exc)))
    except Exception:pass

def report():
    p=log_path();out=[]
    if p.exists():
        with closing(sqlite3.connect(p.as_uri()+'?mode=ro',uri=True,timeout=1)) as c:
            for component,code,kind,n in c.execute('SELECT component,code,category,count FROM errors ORDER BY component,code,category'):
                if component in COMPONENTS and code in CODES and kind in CLASSES and type(n) is int and 0<n<=10**12:
                    out.append({'component':component,'code':code,'category':kind,'count':n})
    return {'version':1,'errors':out}

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description='Privacy-safe error counters');p.add_argument('--agent',action='store_true');p.parse_args()
    try:print(json.dumps(report(),indent=2))
    except Exception:print(json.dumps({'version':1,'errors':[],'available':False}))
