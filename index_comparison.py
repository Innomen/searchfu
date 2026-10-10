"""Explicit, aggregate-only comparison of two stored index inventories.

Private paths are compared inside SQLite and never returned. No source files
are inspected; shared paths establish inventory overlap, not content equality.
"""
import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def compare(primary, secondary):
    primary, secondary = Path(primary).resolve(), Path(secondary).resolve()
    if primary == secondary or not primary.is_file() or not secondary.is_file():
        raise ValueError('invalid_targets')
    with closing(sqlite3.connect(primary.as_uri()+'?mode=ro', uri=True)) as c:
        c.execute('ATTACH DATABASE ? AS secondary', (secondary.as_uri()+'?mode=ro',))
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        out = {}
        for label, schema, path in (('primary', 'main', primary), ('secondary', 'secondary', secondary)):
            out[label] = {
                'files': c.execute(f'SELECT count(*) FROM {schema}.files').fetchone()[0],
                'chunks': c.execute(f'SELECT count(*) FROM {schema}.chunks').fetchone()[0],
                'database_bytes': path.stat().st_size,
                'unused_database_bytes': c.execute(f'PRAGMA {schema}.freelist_count').fetchone()[0] * c.execute(f'PRAGMA {schema}.page_size').fetchone()[0],
                'roots': c.execute(f'SELECT count(DISTINCT root) FROM {schema}.files').fetchone()[0],
            }
        shared = c.execute('SELECT count(*) FROM secondary.files s JOIN main.files p ON p.path=s.path').fetchone()[0]
        out.update(shared_paths=shared, primary_only_paths=out['primary']['files']-shared,
                   secondary_only_paths=out['secondary']['files']-shared,
                   inventories_equal=shared == out['primary']['files'] == out['secondary']['files'],
                   source_tree_walked=False, content_equality_checked=False)
        return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--comparison-target', action='store_true', required=True)
    parser.parse_args()
    try:
        import comparison
        from runtime_paths import pinned_paths
        cfg = comparison.load()
        print(json.dumps(compare(pinned_paths()['SEARCHFU_DB'], cfg['usb_db'])), flush=True)
    except Exception:
        print(json.dumps({'ok': False, 'error': 'inventory_comparison_failed'}), flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
