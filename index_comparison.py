"""Explicit, aggregate-only comparison of two stored index inventories.

Private paths are compared inside SQLite and never returned. No source files
are inspected; shared paths establish inventory overlap, not content equality.
"""
import argparse
import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

# Keep USB as a sequential inventory read and NVMe as the indexed lookup.
# CROSS JOIN preserves this ordering even if future ANALYZE statistics differ.
OVERLAP_SQL = 'SELECT count(*) FROM secondary.files s CROSS JOIN main.files p WHERE p.path=s.path'


def overlap_plan(primary, secondary):
    with closing(sqlite3.connect(Path(primary).resolve().as_uri()+'?mode=ro', uri=True)) as c:
        c.execute('ATTACH DATABASE ? AS secondary', (Path(secondary).resolve().as_uri()+'?mode=ro',))
        rows = c.execute('EXPLAIN QUERY PLAN '+OVERLAP_SQL).fetchall()
        details = [r[3] for r in rows]
        return {'secondary_outer': bool(details and details[0].startswith('SCAN s')),
                'primary_index_lookup': any(d.startswith('SEARCH p') for d in details)}


def compare(primary, secondary, progress=None, counts_only=False):
    primary, secondary = Path(primary).resolve(), Path(secondary).resolve()
    if primary == secondary or not primary.is_file() or not secondary.is_file():
        raise ValueError('invalid_targets')
    with closing(sqlite3.connect(primary.as_uri()+'?mode=ro', uri=True)) as c:
        c.execute('ATTACH DATABASE ? AS secondary', (secondary.as_uri()+'?mode=ro',))
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        out = {}
        for label, schema, path in (('primary', 'main', primary), ('secondary', 'secondary', secondary)):
            if progress: progress({'phase': label+'_inventory'})
            # Never let a diagnostic COUNT silently scan stored passages/vectors
            # when a legacy database has no compact, complete chunk index.
            out[label] = {
                'files': c.execute(f'SELECT count(*) FROM {schema}.files').fetchone()[0],
                'chunks': None,
                'chunk_id_upper_bound': c.execute(f'SELECT COALESCE(MAX(id),0) FROM {schema}.chunks').fetchone()[0],
                'database_bytes': path.stat().st_size,
                'unused_database_bytes': c.execute(f'PRAGMA {schema}.freelist_count').fetchone()[0] * c.execute(f'PRAGMA {schema}.page_size').fetchone()[0],
            }
        if counts_only:
            return out
        if progress: progress({'phase': 'inventory_overlap'})
        shared = c.execute(OVERLAP_SQL).fetchone()[0]
        out.update(shared_paths=shared, primary_only_paths=out['primary']['files']-shared,
                   secondary_only_paths=out['secondary']['files']-shared,
                   inventories_equal=shared == out['primary']['files'] == out['secondary']['files'],
                   source_tree_walked=False, content_equality_checked=False)
        if progress: progress({'phase':'inventory_complete', **out})
        for label, schema in (('primary','main'),('secondary','secondary')):
            indexes = c.execute(f'PRAGMA {schema}.index_list(chunks)').fetchall()
            usable = None
            for row in indexes:
                quoted = '"'+row[1].replace('"','""')+'"'
                cols = [r[2] for r in c.execute(f'PRAGMA {schema}.index_info('+quoted+')')]
                if not row[4] and cols and set(cols) <= {'file_id','ordinal','id'}:
                    usable = quoted
                    break
            if usable:
                # Chunk count is supplementary. Never stall the main inventory
                # answer for an unbounded disk scan, or interrupt retrieval.
                timer = threading.Timer(15, c.interrupt)
                timer.start()
                try:
                    out[label]['chunks'] = c.execute(f'SELECT count(*) FROM {schema}.chunks INDEXED BY '+usable).fetchone()[0]
                except sqlite3.OperationalError:
                    out[label]['chunks'] = None
                finally:
                    timer.cancel(); timer.join()
        return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--comparison-target', action='store_true', required=True)
    parser.add_argument('--plan-only', action='store_true')
    parser.add_argument('--counts-only', action='store_true', help='fast inventory sizes; no overlap or full chunk count')
    args = parser.parse_args()
    try:
        import comparison
        from runtime_paths import pinned_paths
        cfg = comparison.load()
        if args.plan_only:
            print(json.dumps(overlap_plan(pinned_paths()['SEARCHFU_DB'],cfg['usb_db'])), flush=True)
            return 0
        print(json.dumps(compare(pinned_paths()['SEARCHFU_DB'], cfg['usb_db'], lambda value: print(json.dumps(value), flush=True),counts_only=args.counts_only)), flush=True)
    except Exception:
        print(json.dumps({'ok': False, 'error': 'inventory_comparison_failed'}), flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
