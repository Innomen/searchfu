"""Generated indexes only: privacy, actual subprocess boundary and desktop gate."""
import json, os, sqlite3, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import comparison
from fixtures import fixture
from retrieval import retrieve
SOURCE=Path(__file__).resolve().parents[1]

class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.db,self.ann,_=fixture(self.root/'nvme',rows=20,files=5)
        self.usb,self.uann,_=fixture(self.root/'usb',rows=20,files=5)
        self.env=patch.dict(os.environ,{'SEARCHFU_DB':str(self.db),'SEARCHFU_ANN_DIR':str(self.ann),'SEARCHFU_COMPARISON_CONFIG':str(self.root/'config/comparison.json'),'SEARCHFU_METRICS_DB':str(self.root/'state/metrics.sqlite3'),'SEARCHFU_COLLECTIONS':str(self.root/'config/collections.json'),'SEARCHFU_GUI_SOCKET':str(self.root/'state/gui.sock'),'QT_QPA_PLATFORM':'offscreen'})
        self.env.start();comparison.save({'enabled':True,'usb_db':str(self.usb),'usb_ann':str(self.uann),'usb_candidates':'','timeout_seconds':10})
    def tearDown(self):self.env.stop();self.tmp.cleanup()
    def wait_metric(self,name):
        end=time.monotonic()+15
        while time.monotonic()<end:
            v=comparison.report()['modes'].get('keyword',{})
            if v.get(name):return v
            time.sleep(.05)
        self.fail('shadow failed to reach terminal aggregate')
    def test_real_shadow_and_primary_preserved_no_payload_on_disk(self):
        plain=list(retrieve(self.db,'synthetic',refresh=False,fts_only=True,comparison=False))[-1]
        before=self.usb.read_bytes()
        tracked=list(retrieve(self.db,'synthetic',refresh=False,fts_only=True))[-1]
        self.assertEqual(plain['results'],tracked['results'])
        v=self.wait_metric('paired');self.assertEqual(v['overlap_sum'],1)
        self.assertEqual(before,self.usb.read_bytes())
        with sqlite3.connect(comparison.metrics_path()) as c:
            self.assertEqual(c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),[('counters',)])
            raw=json.dumps(c.execute('SELECT * FROM counters').fetchall())
        for secret in ('synthetic',str(self.root),'snippet','result_id',plain['results'][0]['result_id']):self.assertNotIn(secret,raw)
        self.assertEqual(comparison.metrics_path().stat().st_mode&0o777,0o600)
    def test_no_target_no_scan_no_missing_database_creation(self):
        cfg=comparison.load();cfg['usb_db']=str(self.root/'absent.sqlite3');comparison.save(cfg)
        with patch('os.walk',side_effect=AssertionError('no traversal')):
            self.assertEqual(list(retrieve(self.db,'synthetic',refresh=False,fts_only=True))[-1]['stage'],'done')
        self.assertFalse(Path(cfg['usb_db']).exists());self.assertEqual(comparison.report()['modes']['keyword']['missing_target'],1)
    def test_legacy_pinned_scope_is_not_a_zero_hit_speed_comparison(self):
        with sqlite3.connect(self.usb) as c:c.execute('ALTER TABLE files DROP COLUMN source_device')
        scopes={'collections':{'synthetic':{'root':'/synthetic','one_filesystem':True,'device':123}}}
        with patch('collections_config.load',return_value=scopes),patch('comparison.subprocess.Popen',side_effect=AssertionError('must reject before launching')):
            self.assertIsNone(comparison.launch(self.db,'synthetic',{'fts_only':True,'scopes':['synthetic']}))
            self.assertTrue(comparison.scope_supported(self.usb,{'scopes':[]}))
        v=comparison.report()['modes']['keyword']
        self.assertEqual(v['legacy_scope_unsupported'],1);self.assertNotIn('paired',v)
    def test_disabled_and_explicit_optout(self):
        cfg=comparison.load();cfg['enabled']=False;comparison.save(cfg)
        list(retrieve(self.db,'synthetic',refresh=False,fts_only=True))
        self.assertFalse(comparison.metrics_path().exists())
        cfg['enabled']=True;comparison.save(cfg)
        list(retrieve(self.db,'synthetic',refresh=False,fts_only=True,comparison=False))
        self.assertFalse(comparison.metrics_path().exists())
    def test_close_and_failure_cancel_supervisor(self):
        with patch('comparison.launch',return_value='worker'),patch('comparison.finish') as finish:
            g=retrieve(self.db,'synthetic',refresh=False,fts_only=True);next(g);g.close()
            self.assertEqual(finish.call_args.args[:2],('worker','cancelled'))
        with patch('comparison.launch',return_value='worker'),patch('comparison.finish') as finish,patch('retrieval._retrieve_with_refresh',side_effect=RuntimeError('synthetic failure')):
            with self.assertRaises(RuntimeError):list(retrieve(self.db,'synthetic',refresh=False,fts_only=True))
            self.assertEqual(finish.call_args.args[:2],('worker','primary_failed'))
    def test_empty_progress_does_not_count_as_first_result(self):
        events=[{'stage':'keyword','elapsed_seconds':1,'results':[]},
                {'stage':'cancelled','reason':'budget_exhausted','elapsed_seconds':3,'results':[]}]
        with patch('comparison.launch',return_value=None),patch('retrieval._retrieve_with_refresh',return_value=iter(events)):
            list(retrieve(self.db,'synthetic',refresh=False,fts_only=True))
        v=comparison.report()['modes']['keyword']
        self.assertEqual(v['primary_timeouts'],1)
        self.assertEqual(v['primary_first_seconds_sum'],0)
    def test_fixed_export_rejects_unexpected_stored_fields(self):
        comparison.record('keyword','missing_target')
        with sqlite3.connect(comparison.metrics_path()) as c:
            c.execute('INSERT INTO counters VALUES(?,?,?)',('private title','private query',1))
            c.execute('INSERT INTO counters VALUES(?,?,?)',('keyword','private filename',1))
        raw=json.dumps(comparison.report());self.assertNotIn('private',raw)
        with self.assertRaises(ValueError):comparison.record('private query','paired')
    def test_footprint_counts_only_index_does_not_follow_links(self):
        (self.root/'usb/ann/private-link').symlink_to(self.root/'nvme',target_is_directory=True)
        report=comparison.footprint(self.usb,self.uann)
        self.assertTrue(report['available']);self.assertGreater(report['cache_bytes'],0)
        self.assertEqual(report['database_bytes'],self.usb.stat().st_size)
        self.assertNotIn(str(self.root),json.dumps(report))
    def test_browse_literal_filter_pagination_no_walk(self):
        from gui import browse
        with patch('os.walk',side_effect=AssertionError('no source walks')):
            first=browse(self.db,limit=2);second=browse(self.db,after=first[-1][0],limit=2)
            self.assertEqual(len(first),2);self.assertTrue(first[-1][0]<second[0][0]);self.assertEqual(browse(self.db,text='%'),[])
    def test_generic_fixture_primary_never_touches_live_comparison_settings(self):
        with patch.dict(os.environ,{'SEARCHFU_COMPARISON_CONFIG':''}),patch('comparison.launch',side_effect=AssertionError('live config touched')):
            self.assertEqual(list(retrieve(self.db,'synthetic',refresh=False,fts_only=True))[-1]['stage'],'done')
    def test_runtime_settings_and_metrics_cannot_live_in_source(self):
        with patch.dict(os.environ,{'SEARCHFU_COMPARISON_CONFIG':str(SOURCE/'comparison.json')}):
            with self.assertRaises(ValueError):comparison.config_path()
        with patch.dict(os.environ,{'SEARCHFU_METRICS_DB':str(SOURCE/'metrics.sqlite3')}):
            with self.assertRaises(ValueError):comparison.metrics_path()
    def test_busy_shadow_is_skipped_without_backlog(self):
        import fcntl
        lock=comparison.metrics_path().parent/'comparison.lock';lock.parent.mkdir(parents=True,exist_ok=True)
        with lock.open('w') as out:
            fcntl.flock(out,fcntl.LOCK_EX|fcntl.LOCK_NB)
            list(retrieve(self.db,'synthetic',refresh=False,fts_only=True))
            self.wait_metric('busy')
    def test_detached_supervisor_enforces_hard_timeout(self):
        cfg=comparison.load();cfg['timeout_seconds']=1
        request={'config':cfg,'query':'synthetic','options':{'fts_only':True},'mode':'keyword'}
        primary={'outcome':'paired','primary':{'seconds':.1,'first_seconds':.01,'count':0,'ids':[]}}
        # Substitute a deliberately wedged synthetic child at the actual process
        # boundary; supervisor must kill/reap it rather than just mark a timeout.
        program="""import comparison,subprocess,sys
original=subprocess.Popen
def wedged(args,**kw):
    return original([sys.executable,'-c','import sys,time;sys.stdin.read();time.sleep(30)'],**kw)
comparison.subprocess.Popen=wedged
comparison.supervisor()
"""
        started=time.monotonic()
        result=subprocess.run([sys.executable,'-c',program],cwd=SOURCE,input=json.dumps(request)+'\n'+json.dumps(primary)+'\n',capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr);self.assertLess(time.monotonic()-started,4)
        self.assertEqual(comparison.report()['modes']['keyword']['shadow_timeout'],1)
    def test_supervisor_cancellation_owns_child_exit(self):
        p=comparison.launch(self.db,'synthetic',{'fts_only':True,'refresh':False})
        comparison.finish(p,'cancelled');self.wait_metric('cancelled');p.wait(timeout=5)
        self.assertEqual(p.returncode,0)
    def test_worker_reports_scan_before_blocking_retrieval(self):
        import io, gui
        output=io.StringIO()
        request={'db':str(self.db),'query':'private synthetic phrase','options':{'fts_only':True}}
        def slow_retrieval(*args,**kwargs):
            # These notifications must already be available before any SQL work.
            events=[json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual([e['stage'] for e in events],['opening','keyword_scan'])
            self.assertNotIn(request['query'],output.getvalue())
            self.assertNotIn(str(self.db),output.getvalue())
            yield {'stage':'done','results':[]}
        with patch('sys.stdin',io.StringIO(json.dumps(request))),patch('sys.stdout',output),patch('signal.signal'),patch('retrieval.retrieve',slow_retrieval):gui.worker()

    def test_installed_shell_flags_and_native_gui_actual_search(self):
        # Pin shell to fixtures exactly as installed launcher does.
        env={**os.environ,'SEARCHFU_PY':sys.executable,'SEARCHFU_DIR':str(self.db.parent),'SEARCHFU_CANDIDATES_DIR':str(self.db.parent/'candidates')}
        cli=subprocess.run(['bash',str(SOURCE/'searchfu.sh'),'search','synthetic','--fts','--no-refresh','--no-comparison'],env=env,capture_output=True,text=True,timeout=20)
        self.assertEqual(cli.returncode,0,cli.stderr);self.assertTrue(json.loads(cli.stdout))
        # GUI main honors the installed pinned primary; synthetic override is a
        # direct entrypoint test instead of touching the user's primary index.
        program="import gui,sys; sys.argv=['gui','--smoke-test']; sys.exit(gui.main("+repr(str(self.db))+"))"
        gui=subprocess.run([sys.executable,'-c',program],cwd=SOURCE,env=env,capture_output=True,text=True,timeout=25)
        self.assertEqual(gui.returncode,0,gui.stderr)
        self.wait_metric('paired')

if __name__=='__main__':unittest.main()
