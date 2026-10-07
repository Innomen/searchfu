"""Pinned installed index locations; generic developer APIs remain configurable.

No backup discovery, import or fallback. Installed consumers ignore inherited
index-location overrides and share this one authoritative path family.
"""
from pathlib import Path
import os, shlex


def pinned_paths(home=None):
    data=Path(home if home is not None else Path.home())/'.local/share/searchfu'
    return {'SEARCHFU_DIR':str(data),'SEARCHFU_DB':str(data/'drive.sqlite3'),
            'SEARCHFU_ANN_DB':str(data/'drive.sqlite3'),'SEARCHFU_ANN_DIR':str(data/'ann'),
            'SEARCHFU_CANDIDATES_DIR':str(data/'candidates')}


def activate(home=None):
    paths=pinned_paths(home)
    os.environ.update(paths)
    return paths


def launcher_text(app,env,home=None):
    exports={**pinned_paths(home),'SEARCHFU_PY':str(Path(env)/'bin/python')}
    return ('#!/usr/bin/env bash\nset -euo pipefail\n'
            + ''.join('export '+key+'='+shlex.quote(value)+'\n' for key,value in exports.items())
            + 'exec bash '+shlex.quote(str(Path(app)/'searchfu.sh'))+' "$@"\n')
