import contextlib,errno,io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import search,indexing_health

class IndexingHealthTests(unittest.TestCase):
    def test_walk_causes_stay_local_and_success_cannot_erase_failed_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);db=root/'index.db';private=root/'synthetic-private-target'
            def failed_walk(base,**kw):
                kw['onerror'](PermissionError(errno.EACCES,'denied',str(private)))
                kw['onerror'](FileNotFoundError(errno.ENOENT,'gone',str(private)))
                kw['onerror'](OSError(errno.EIO,'io',str(private)))
                return iter([])
            output=io.StringIO()
            with patch('os.walk',side_effect=failed_walk),contextlib.redirect_stdout(output):search.build(db,[str(root)],images=False,names_only=True)
            self.assertNotIn(str(private),output.getvalue())
            final=json.loads(output.getvalue().splitlines()[-1])
            self.assertFalse(final['coverage_complete'])
            self.assertEqual(final['walk_error_categories'],{'permission':1,'disappeared':1,'io':1})
            with patch('os.walk',return_value=iter([])),contextlib.redirect_stdout(io.StringIO()):search.build(db,[str(root)],images=False,names_only=True)
            c=search.connect(db,readonly=True)
            try:
                reports=indexing_health.summaries(c)
                self.assertFalse(reports[0]['coverage_complete'])
                self.assertTrue(reports[1]['coverage_complete'])
                self.assertEqual(reports[0]['walk_errors'],3)
                self.assertNotIn(str(private),json.dumps(reports))
                self.assertEqual(c.execute('SELECT count(*) FROM indexing_failures WHERE target=?',(str(private),)).fetchone()[0],3)
            finally:c.close()
            from retrieval import retrieve
            event=list(retrieve(db,'synthetic',fts_only=True))[-1]
            self.assertEqual(event['index_health']['indexing_runs'][0]['walk_errors'],3)
    def test_unknown_legacy_failures_are_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=search.connect(Path(tmp)/'index.db')
            try:
                rid=c.execute('INSERT INTO indexing_runs(started,names_only,limited) VALUES(1,1,0)').lastrowid
                errors=indexing_health.WalkFailures(c,rid);errors.append(True)
                self.assertEqual(errors,['unknown'])
                self.assertEqual(indexing_health.summaries(c)[0]['walk_error_categories'],{'unknown':1})
            finally:c.close()

    def test_root_stat_permission_is_not_mislabeled_as_missing(self):
        failures=[]
        class Recorder(list):
            def record(self,exc=None,target=None):self.append(indexing_health.category(exc))
        failures=Recorder()
        with patch.object(Path,'stat',side_effect=PermissionError(errno.EACCES,'denied')):
            self.assertEqual(list(search.iter_files(['/synthetic-root'],failures)),[])
        self.assertEqual(failures,['permission'])
