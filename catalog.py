#!/usr/bin/env python3
"""Explicit collection management; only build/update traverses source roots."""
from pathlib import Path
import argparse,json,os
import collections_config as config
import search


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--db',type=Path,default=search.profile_db('vault'))
    sub=ap.add_subparsers(dest='cmd',required=True)
    p=sub.add_parser('add');p.add_argument('name');p.add_argument('root');p.add_argument('--priority',type=int,default=100);p.add_argument('--one-filesystem',action='store_true');p.add_argument('--offline',action='store_true');p.add_argument('--exclude',action='append',default=[])
    p=sub.add_parser('list');p.add_argument('--agent',action='store_true')
    for cmd in ('build','update'):
        p=sub.add_parser(cmd);p.add_argument('names',nargs='+');p.add_argument('--allow-model-download',action='store_true');p.add_argument('--names-only',action='store_true');p.add_argument('--max-files',type=int,default=0)
    a=ap.parse_args();data=config.load()
    if a.cmd=='add':
        import re
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}',a.name): raise ValueError('invalid_scope_name')
        root=Path(a.root).absolute()
        entry={'root':str(root),'priority':a.priority,'searchable':not a.offline,'one_filesystem':a.one_filesystem,'exclude':[str(Path(x).absolute()) for x in a.exclude]}
        if not a.offline:
            st=root.stat()
            if not root.is_dir(): raise ValueError('scope_root_not_directory')
            entry['device']=st.st_dev
            if a.one_filesystem:entry['devices']=config.mounted_devices(root)
        data['collections'][a.name]=entry;config.save(data)
        print(json.dumps({'ok':True,'configured_scopes':len(data['collections'])}))
    elif a.cmd=='list':
        if a.agent:
            print(json.dumps({'configured_scopes':len(data['collections']),'searchable_scopes':sum(bool(e.get('searchable',True)) for e in data['collections'].values())}))
        else: print(json.dumps(data,indent=2))
    else:
        roots=[];excluded=[];devices={}
        for name in a.names:
            if name not in data['collections']: raise ValueError('unknown_scope')
            entry=data['collections'][name]
            if not entry.get('searchable',True): raise ValueError('scope_offline')
            root=Path(entry['root']);st=root.stat()
            if st.st_dev!=entry.get('device'): raise ValueError('scope_device_changed')
            roots.append(str(root));excluded.extend(entry.get('exclude',[]))
            if entry.get('one_filesystem'):devices[str(root)]=entry.get('devices',[st.st_dev])
        # Exclude private runtime/model caches and our own index, including when
        # data resides under the configured root. Never index an index recursively.
        excluded.extend([str(a.db.parent.absolute()),str(config.config_path().parent.absolute()),
                         str(Path.home()/'.cache'),str(Path.home()/'.local/share/searchfu')])
        search.build(a.db,roots,a.allow_model_download,False,a.max_files,excluded=excluded,devices=devices,names_only=a.names_only)

if __name__=='__main__':
    os.umask(0o077)
    try:main()
    except Exception as exc:
        print(json.dumps({'ok':False,'error':'collection_operation_failed','exception_class':type(exc).__name__}));raise SystemExit(1)
