import sqlite3
import tempfile
import unittest
from pathlib import Path
from index_comparison import compare


class InventoryComparisonTests(unittest.TestCase):
    def test_different_inventory_is_not_a_storage_only_comparison(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory)/name for name in ('primary.db', 'secondary.db')]
            for path, names in zip(paths, [('common', 'primary-only'), ('common', 'secondary-only')]):
                with sqlite3.connect(path) as c:
                    c.executescript('CREATE TABLE files(path TEXT UNIQUE,root TEXT); CREATE TABLE chunks(id INTEGER PRIMARY KEY);')
                    c.executemany('INSERT INTO files VALUES(?,?)', [(name, 'synthetic-root') for name in names])
                    c.execute('INSERT INTO chunks VALUES(1)')
            before = [p.read_bytes() for p in paths]
            result = compare(*paths)
            self.assertEqual(result['shared_paths'], 1)
            self.assertEqual(result['primary_only_paths'], 1)
            self.assertEqual(result['secondary_only_paths'], 1)
            self.assertFalse(result['inventories_equal'])
            self.assertFalse(result['content_equality_checked'])
            self.assertNotIn('common', str(result))
            self.assertEqual(before, [p.read_bytes() for p in paths])
            with sqlite3.connect(paths[1]) as c:
                c.execute("UPDATE files SET path='primary-only' WHERE path='secondary-only'")
            self.assertTrue(compare(*paths)['inventories_equal'])
