"""Named views over a canonical index. Configuration and source paths stay local."""
from pathlib import Path
import json
import os
import re


def config_path():
    return Path(os.environ.get('SEARCHFU_COLLECTIONS',str(Path(os.environ.get('XDG_CONFIG_HOME',str(Path.home()/'.config')))/'searchfu/collections.json')))


def load(path=None):
    target=Path(path) if path else config_path()
    if not target.exists(): return {'version':1,'collections':{}}
    data=json.loads(target.read_text())
    if data.get('version')!=1 or not isinstance(data.get('collections'),dict): raise ValueError('invalid_collections')
    for name,entry in data['collections'].items():
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}',name): raise ValueError('invalid_scope_name')
        if not isinstance(entry,dict) or not Path(entry.get('root','')).is_absolute(): raise ValueError('invalid_scope_root')
        if not isinstance(entry.get('priority',100),int): raise ValueError('invalid_scope_priority')
    return data


def save(data,path=None):
    target=Path(path) if path else config_path()
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temp=target.with_suffix('.tmp')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as out: json.dump(data,out,indent=2);out.write('\n')
    os.replace(temp,target)


def scope_sql(names,config=None):
    """No source stat, existence check or traversal during query."""
    if not names: return [],[],{}
    data=load(config)['collections']
    selected={}
    for name in dict.fromkeys(names):
        if name not in data: raise ValueError('unknown_scope')
        if not data[name].get('searchable',True): raise ValueError('scope_not_searchable')
        selected[name]=data[name]
    clauses=[];params=[]
    for entry in selected.values():
        root=entry['root'].rstrip('/') or '/'
        clauses.append('(f.path=? OR substr(f.path,1,?)=?)')
        prefix=root.rstrip('/')+'/'
        params.extend([root,len(prefix),prefix])
        # Mount boundaries are stored in the catalog at indexing; a root view
        # must not accidentally include another filesystem's separately indexed rows.
        if entry.get('one_filesystem',False):
            devices=entry.get('devices',[entry.get('device',-1)])
            clauses[-1]='('+clauses[-1]+' AND f.source_device IN ('+','.join('?' for _ in devices)+'))'
            params.extend(devices)
    return ['('+' OR '.join(clauses)+')'],params,selected


def memberships(path,selected,device=None):
    return [name for name,e in selected.items() if (path==e['root'] or path.startswith(e['root'].rstrip('/')+'/')) and
            (not e.get('one_filesystem') or device in e.get('devices',[e.get('device')]))]


def mounted_devices(root):
    """At explicit scope configuration only, include subvolumes of one NVMe.

    Btrfs subvolumes can have different st_dev despite sharing one drive.
    Mount source names and private mount points never leave this local helper.
    """
    def unescape(value):
        return re.sub(r'\\([0-7]{3})',lambda m:chr(int(m[1],8)),value)
    mounts=[]
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        left,right=line.split(' - ',1);a=left.split();b=right.split()
        mounts.append((unescape(a[4]),unescape(b[1])))
    covering=[(p,s) for p,s in mounts if str(root)==p or str(root).startswith(p.rstrip('/')+'/')]
    source=max(covering,key=lambda x:len(x[0]))[1] if covering else None
    base=re.sub(r'p[0-9]+$','',source or '')
    devices={Path(root).stat().st_dev}
    for point,other in mounts:
        if other==source or (base.startswith('/dev/nvme') and re.sub(r'p[0-9]+$','',other)==base):
            try:devices.add(Path(point).stat().st_dev)
            except OSError:pass
    return sorted(devices)
