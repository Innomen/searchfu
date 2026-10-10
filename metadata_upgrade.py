"""Explicit, resumable metadata repair; never extracts text or embeds content.

Private catalog paths stay inside this local operation. Its progress/report
envelope contains numeric counts and booleans only. Search never calls it.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
from contextlib import ExitStack


def upgrade(db, ann, progress=None, batch_size=1000):
    db, ann = Path(db), Path(ann)
    if not db.is_file() or not ann.is_dir():
        raise ValueError('missing_target')
    # Cooperate with index/cache writers; do not create or rewrite cache data.
    with ExitStack() as stack:
        writer = stack.enter_context(open(str(db)+'.update.lock','a'))
        fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock = stack.enter_context(open(ann / '.build.lock', 'a'))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        c = sqlite3.connect(db, timeout=30)
        try:
            c.execute('PRAGMA busy_timeout=30000')
            with c:
                c.execute('BEGIN IMMEDIATE')
                columns = {r[1] for r in c.execute('PRAGMA table_info(files)')}
                for name, kind in (('extractor', "TEXT NOT NULL DEFAULT 'plain-text-v1'"),
                                   ('content_hash', 'TEXT'), ('source_device', 'INTEGER')):
                    if name not in columns:
                        c.execute('ALTER TABLE files ADD COLUMN '+name+' '+kind)
                c.execute('CREATE TABLE IF NOT EXISTS index_state(key TEXT PRIMARY KEY,value INTEGER NOT NULL)')
                identity = c.execute("SELECT value FROM index_state WHERE key='index_id'").fetchone()
                meta_path = ann / 'ann.meta.json'
                meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
                cached_id = meta.get('index_id')
                if cached_id is not None and (not isinstance(cached_id, str) or not cached_id or len(cached_id)>128):
                    raise ValueError('invalid_cache_identity')
                if identity and cached_id and str(identity[0]) != cached_id:
                    raise ValueError('matrix_index_mismatch')
                if not identity:
                    c.execute("INSERT INTO index_state VALUES('index_id',?)", (cached_id or uuid.uuid4().hex,))
                high = c.execute('SELECT COALESCE(MAX(id),0) FROM chunks').fetchone()[0]
                c.execute("INSERT INTO index_state VALUES('chunk_high_water',?) ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)", (high,))
                c.execute("CREATE TRIGGER IF NOT EXISTS chunks_water AFTER INSERT ON chunks BEGIN UPDATE index_state SET value=MAX(value,new.id) WHERE key='chunk_high_water'; END")
                # Presence alone must not make a partly backfilled legacy scope
                # look like a valid zero-hit benchmark.
                c.execute("INSERT OR REPLACE INTO index_state VALUES('metadata_device_upgrade',1)")
            counts = {'checked':0, 'updated':0, 'unavailable':0}
            last = 0
            started = time.monotonic()
            while True:
                rows = c.execute('SELECT id,path FROM files WHERE id>? AND source_device IS NULL ORDER BY id LIMIT ?', (last,batch_size)).fetchall()
                if not rows:
                    break
                changes = []
                for file_id, path in rows:
                    try:
                        device = os.stat(path).st_dev
                    except (OSError, ValueError, TypeError):
                        counts['unavailable'] += 1
                    else:
                        changes.append((device,file_id))
                    counts['checked'] += 1
                    last = file_id
                with c:
                    c.executemany('UPDATE files SET source_device=? WHERE id=? AND source_device IS NULL',changes)
                counts['updated'] += len(changes)
                if progress:
                    progress({**counts,'elapsed_seconds':round(time.monotonic()-started,2)})
            remaining = c.execute('SELECT count(*) FROM files WHERE source_device IS NULL').fetchone()[0]
            return {**counts,'remaining_unknown':remaining,'complete':remaining==0,
                    'cache_identity_preserved':not cached_id or str(c.execute("SELECT value FROM index_state WHERE key='index_id'").fetchone()[0])==cached_id,
                    'elapsed_seconds':round(time.monotonic()-started,2)}
        finally:
            c.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--comparison-target', action='store_true', required=True)
    args = parser.parse_args()
    import comparison
    cfg = comparison.load()
    last_report = [0.0]
    def progress(report):
        if time.monotonic()-last_report[0] >= 30:
            print(json.dumps(report),flush=True)
            last_report[0] = time.monotonic()
    try:
        lockpath=comparison.metrics_path().parent/'comparison.lock'
        lockpath.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd=os.open(lockpath,os.O_CREAT|os.O_RDWR,0o600)
        with os.fdopen(fd,'w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            report = upgrade(cfg['usb_db'],cfg['usb_ann'],progress)
    except Exception:
        # No traceback, private paths or exception formatting at this boundary.
        print(json.dumps({'completed':False,'error':'metadata_upgrade_failed'}),flush=True)
        return 1
    print(json.dumps({'completed':True,**report}),flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
