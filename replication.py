"""Owner-requested local snapshot replication; never emits private data.

The canonical SQLite database and its matching caches travel together. Existing
target artifacts are retained as a rollback copy. This is not source indexing.
"""
from contextlib import ExitStack, closing
from pathlib import Path
import argparse
import fcntl
import json
import os
import shutil
import sqlite3
import time
import uuid


def configured_paths(cfg):
    from runtime_paths import pinned_paths
    p = pinned_paths()
    db = Path(cfg['usb_db'])
    return ({'db': p['SEARCHFU_DB'], 'ann': p['SEARCHFU_ANN_DIR'], 'candidates': p['SEARCHFU_CANDIDATES_DIR']},
            {'db': db, 'ann': cfg['usb_ann'] or db.parent/'ann', 'candidates': cfg['usb_candidates'] or db.parent/'candidates'})


def _signature(paths):
    result = {}
    for key, raw in paths.items():
        p = Path(raw).resolve()
        probes = [p, Path(str(p)+'-wal')] if key == 'db' else [p/'ann.meta.json' if key == 'ann' else p/'candidates.meta.json']
        result[key] = {'path': str(p), 'files': []}
        for probe in probes:
            try:
                s = probe.stat()
                # Opening a WAL-mode DB read-only may create an empty WAL.
                # Empty and absent journals carry the same committed content.
                result[key]['files'].append(None if key == 'db' and probe != p and s.st_size == 0 else [s.st_size, s.st_mtime_ns])
            except FileNotFoundError:
                result[key]['files'].append(None)
    return result


def snapshot_matches(cfg, primary_db=None):
    """Conservative replica freshness check; paths/stamps never leave the host."""
    try:
        if not cfg['usb_db']: return False
        source, target = configured_paths(cfg)
        if primary_db is not None and Path(primary_db).resolve() != Path(source['db']).resolve(): return False
        manifest = json.loads(Path(str(target['db'])+'.replica.json').read_text())
        return manifest.get('version') == 1 and manifest.get('source') == _signature(source) and manifest.get('target') == _signature(target)
    except (OSError, ValueError, TypeError):
        return False


def _tree_bytes(path):
    if not path.exists():
        return 0
    if path.is_symlink():
        raise ValueError('symlink_artifact')
    if path.is_file():
        return path.stat().st_size
    return sum(_tree_bytes(p) for p in path.iterdir())


def _sync(path):
    if path.is_dir():
        for child in path.iterdir():
            _sync(child)
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def replicate(source, target, progress=None):
    """Call under the comparison lock: no live target readers during publish.

    Keys are db/ann/candidates. Writer locks hold the source generation stable;
    SQLite backup includes committed WAL instead of copying a live DB file.
    Staged files never replace the old target until all artifacts are durable.
    """
    source = {k: Path(source[k]).resolve() for k in ('db', 'ann', 'candidates')}
    target = {k: Path(target[k]).absolute() for k in source}
    allpaths = list(source.values()) + list(target.values())
    if len(set(allpaths)) != 6 or any(a.is_relative_to(b) for a in allpaths for b in allpaths if a != b):
        raise ValueError('overlapping_artifacts')
    if not source['db'].is_file() or not source['ann'].is_dir():
        raise ValueError('missing_source')
    if any(p.is_symlink() for p in target.values()):
        raise ValueError('symlink_target')
    started = time.monotonic()
    report = progress or (lambda value: None)
    token = uuid.uuid4().hex
    stages = {k: p.with_name('.searchfu-replica-'+token+'-'+k) for k, p in target.items()}
    backups = {k: p.with_name(p.name+'.before-replica-'+token) for k, p in target.items()}
    moved, installed = [], []
    with ExitStack() as stack:
        for path in (Path(str(source['db'])+'.update.lock'), Path(str(source['db'])+'.build.lock'), source['ann']/'.build.lock'):
            lock = stack.enter_context(path.open('a'))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if source['candidates'].is_dir():
            lock = stack.enter_context((source['candidates']/'.build.lock').open('a'))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for path in (Path(str(target['db'])+'.update.lock'), Path(str(target['db'])+'.build.lock')):
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock = stack.enter_context(path.open('a'))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for key in ('ann', 'candidates'):
            if target[key].is_dir():
                lock = stack.enter_context((target[key]/'.build.lock').open('a'))
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        source_before = _signature(source)
        # Fail before copying if a source artifact includes an unexpected link.
        sizes = {k: _tree_bytes(p) for k, p in source.items()}
        required_by_device = {}
        for key, p in target.items():
            p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            dev = p.parent.stat().st_dev
            required_by_device[dev] = required_by_device.get(dev, 0) + sizes[key]
        for p in target.values():
            if shutil.disk_usage(p.parent).free < required_by_device[p.parent.stat().st_dev] + 8*1024**3:
                raise ValueError('insufficient_capacity')
        try:
            last = [0.0]
            def pages(status, remaining, total):
                now = time.monotonic()
                if now-last[0] >= 30 or remaining == 0:
                    report({'phase': 'copy_database', 'pages_copied': total-remaining,
                            'pages_total': total, 'elapsed_seconds': round(now-started, 2)})
                    last[0] = now
            with closing(sqlite3.connect(source['db'].as_uri()+'?mode=ro', uri=True)) as src:
                src.execute('BEGIN')
                src.execute('SELECT count(*) FROM sqlite_master').fetchone()
                with closing(sqlite3.connect(stages['db'])) as dst:
                    src.backup(dst, pages=4096, progress=pages)
                    dst.execute('PRAGMA journal_mode=DELETE')
                    # Header/schema and exact page transfer, not a content scan.
                    if dst.execute('PRAGMA page_count').fetchone() != src.execute('PRAGMA page_count').fetchone():
                        raise ValueError('backup_verification_failed')
                    src_id = src.execute("SELECT value FROM index_state WHERE key='index_id'").fetchone()
                    if not src_id or dst.execute("SELECT value FROM index_state WHERE key='index_id'").fetchone() != src_id:
                        raise ValueError('backup_identity_failed')
            stages['db'].chmod(0o600)
            for key in ('ann', 'candidates'):
                report({'phase': 'copy_'+key, 'elapsed_seconds': round(time.monotonic()-started, 2)})
                if source[key].exists():
                    shutil.copytree(source[key], stages[key])
                else:
                    stages[key].mkdir(mode=0o700)
                stages[key].chmod(0o700)
                for parent, dirs, files in os.walk(stages[key]):
                    Path(parent).chmod(0o700)
                    for name in files: Path(parent, name).chmod(0o600)
                if _tree_bytes(stages[key]) != sizes[key]:
                    raise ValueError('cache_copy_verification_failed')
            for key, name in (('ann', 'ann.meta.json'), ('candidates', 'candidates.meta.json')):
                meta = stages[key]/name
                if meta.exists() and json.loads(meta.read_text()).get('index_id') != str(src_id[0]):
                    raise ValueError('cache_identity_mismatch')
            for path in stages.values():
                _sync(path)
            if _signature(source) != source_before:
                raise ValueError('source_changed_during_copy')
            # Keep sidecars with the old database; none may attach to the new DB.
            for suffix in ('-wal', '-shm'):
                old = Path(str(target['db'])+suffix)
                if old.exists():
                    saved = Path(str(backups['db'])+suffix)
                    old.rename(saved)
                    moved.append((old, saved))
            for key in source:
                if target[key].exists():
                    target[key].rename(backups[key])
                    moved.append((target[key], backups[key]))
                stages[key].rename(target[key])
                installed.append(target[key])
            for parent in set(p.parent for p in target.values()):
                fd = os.open(parent, os.O_RDONLY)
                try: os.fsync(fd)
                finally: os.close(fd)
            manifest = Path(str(target['db'])+'.replica.json')
            temporary = manifest.with_name('.replica-manifest-'+token)
            try:
                temporary.write_text(json.dumps({'version': 1, 'source': _signature(source), 'target': _signature(target)}))
                temporary.chmod(0o600)
                _sync(temporary)
                temporary.replace(manifest)
            finally:
                temporary.unlink(missing_ok=True)
            return {'completed': True, 'old_copy_preserved': bool(moved),
                    'database_bytes': target['db'].stat().st_size,
                    'cache_bytes': sizes['ann']+sizes['candidates'],
                    'source_tree_walked': False, 'reembedded': False,
                    'elapsed_seconds': round(time.monotonic()-started, 2)}
        except Exception:
            for path in reversed(installed):
                if path.is_dir(): shutil.rmtree(path)
                else: path.unlink()
            for old, saved in reversed(moved):
                saved.rename(old)
            raise
        finally:
            for path in stages.values():
                if path.is_dir(): shutil.rmtree(path)
                else: path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--comparison-target', action='store_true', required=True)
    parser.add_argument('--wait', action='store_true', help='let an active experiment finish first')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        import comparison
        cfg = comparison.load()
        db = Path(cfg['usb_db'])
        if not cfg['usb_db'] or not db.is_file(): raise ValueError('missing_target')
        source, target = configured_paths(cfg)
        lockpath = comparison.metrics_path().parent/'comparison.lock'
        lockpath.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with lockpath.open('a') as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not args.wait: raise
                    print(json.dumps({'phase': 'waiting_for_experiment'}), flush=True)
                    time.sleep(30)
            print(json.dumps(replicate(source, target, lambda value: print(json.dumps(value), flush=True))), flush=True)
    except Exception:
        print(json.dumps({'completed': False, 'error': 'replication_failed'}), flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
