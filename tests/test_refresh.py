"""Read repair regressions using generated content only."""
import contextlib, fcntl, json, os, subprocess, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import search, refresh, retrieval

class RepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        (self.root/'index').mkdir();(self.root/'corpus').mkdir()
        self.db=self.root/'index/db.sqlite3';self.path=self.root/'corpus/a.txt'
        self.old='Old amber evidence. '*8;self.new='Fresh violet discovery. '*8
        self.path.write_text(self.old)
        c=search.connect(self.db);st=self.path.stat()
        c.execute('INSERT INTO files(id,path,root,kind,content_sig,source_device) VALUES(1,?,?,?, ?,?)',(str(self.path),str(self.path.parent),'text',search.file_sig(st),st.st_dev))
        c.execute('INSERT INTO chunks(id,file_id,ordinal,text,embedding) VALUES(1,1,0,?,?)',(self.old,search.pack(np.ones(384,dtype=np.float32))))
        c.commit();c.close();self.released=False
        (self.root/'config').mkdir();self.config=self.root/'config/collections.json';self.config.write_text('{"version":1,"collections":{}}')
        self.request={'db':str(self.db),'paths':[],'options':{'collections_file':str(self.config)}}
    def tearDown(self):self.tmp.cleanup()
    @contextlib.contextmanager
    def lease(self):
        try:yield lambda:None
        finally:self.released=True
    def encode(self,texts):return np.ones((len(texts),384),dtype=np.float32)
    def repair(self,**kw):return refresh.repair(self.request,encoder=kw.pop('encoder',self.encode),admitted=self.lease,**kw)
    def contents(self):
        c=search.connect(self.db,readonly=True)
        try:return c.execute('SELECT text FROM chunks').fetchall()
        finally:c.close()
    def test_no_change_never_requests_lease(self):
        with patch.object(self,'lease',side_effect=AssertionError('unexpected lease')):
            self.assertEqual(self.repair()['updated'],0)
    def test_rotating_sample_finds_new_relevance_and_removes_old(self):
        self.path.write_text(self.new)
        report=self.repair();self.assertEqual(report['updated'],1);self.assertTrue(self.released)
        self.assertIn('Fresh violet',self.contents()[0][0])
        self.assertEqual(list(retrieval.retrieve(self.db,'amber',fts_only=True))[-1]['results'],[])
        self.assertEqual(len(list(retrieval.retrieve(self.db,'violet',fts_only=True))[-1]['results']),1)
    def test_failed_encode_preserves_evidence_and_releases(self):
        self.path.write_text(self.new)
        def fail(_):raise RuntimeError('synthetic')
        self.assertEqual(self.repair(encoder=fail)['status'],'refresh_failed')
        self.assertEqual(self.contents(),[(self.old,)]);self.assertTrue(self.released)
    def test_denied_gpu_admission_preserves_evidence(self):
        self.path.write_text(self.new)
        @contextlib.contextmanager
        def denied():
            raise RuntimeError('gpu_lease_unavailable')
            yield
        report=refresh.repair(self.request,encoder=self.encode,admitted=denied)
        self.assertEqual(report['status'],'gpu_unavailable')
        self.assertEqual(self.contents(),[(self.old,)])

    def test_cancel_during_encode_preserves_evidence(self):
        self.path.write_text(self.new);stop=[False]
        def encode(texts):stop[0]=True;return self.encode(texts)
        self.assertEqual(self.repair(encoder=encode,cancelled=lambda:stop[0])['status'],'cancelled')
        self.assertEqual(self.contents(),[(self.old,)]);self.assertTrue(self.released)
    def test_file_changes_during_encode(self):
        self.path.write_text(self.new)
        def encode(texts):self.path.write_text('Another concurrent edit. '*8);return self.encode(texts)
        self.assertEqual(self.repair(encoder=encode)['updated'],0)
        self.assertEqual(self.contents(),[(self.old,)])
    def test_symlink_parent_is_rejected(self):
        link=self.root/'alias';link.symlink_to(self.path.parent,target_is_directory=True)
        with self.assertRaises(OSError):refresh.safe_read(link/'a.txt')
    def test_busy_writer_is_skipped(self):
        with open(str(self.db)+'.build.lock','a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            self.assertEqual(self.repair()['status'],'writer_busy')
    def test_wrapper_refreshes_snapshot_and_delta_removals(self):
        self.path.write_text(self.new)
        with patch.object(refresh,'run',side_effect=lambda *_:self.repair()):
            events=list(retrieval.retrieve(self.db,'amber',fts_only=True,refresh=True,deltas=True))
        self.assertEqual(events[-1]['stage'],'done');self.assertEqual(events[-1]['results'],[])
        self.assertTrue(any(e.get('changes',{}).get('removed') for e in events))
        self.assertEqual(sum(e['stage']=='done' for e in events),1)
    def test_existing_schema_needs_no_beta_migration(self):
        def schema():
            c=search.connect(self.db,readonly=True)
            try:return c.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
            finally:c.close()
        before=schema();self.path.write_text(self.new)
        self.assertEqual(self.repair()['updated'],1)
        self.assertEqual(schema(),before)

    def test_reserved_ann_ids_are_never_reused(self):
        ann=self.db.parent/'ann';ann.mkdir();(ann/'ann.meta.json').write_text('{"db_max_rowid":9000}')
        self.path.write_text(self.new)
        with patch.dict(os.environ,{'SEARCHFU_ANN_DIR':str(ann)}):self.repair()
        c=search.connect(self.db,readonly=True)
        try:self.assertGreater(c.execute('SELECT min(id) FROM chunks').fetchone()[0],9000)
        finally:c.close()
    def test_empty_file_removes_old_chunks_without_gpu(self):
        self.path.write_text('')
        with patch.object(self,'lease',side_effect=AssertionError('unexpected lease')):
            self.assertEqual(self.repair()['updated'],1)
        self.assertEqual(self.contents(),[])
    def test_excluded_file_preserves_old_evidence(self):
        self.config.write_text(json.dumps({'version':1,'collections':{'synthetic':{'root':str(self.path.parent),'exclude':[str(self.path)]}}}))
        self.path.write_text(self.new)
        self.assertEqual(self.repair()['updated'],0)
        self.assertEqual(self.contents(),[(self.old,)])

    def test_refresh_launch_failure_preserves_search_results(self):
        with patch.object(refresh,'run',side_effect=OSError('synthetic spawn failure')):
            terminal=list(retrieval.retrieve(self.db,'amber',fts_only=True,refresh=True))[-1]
        self.assertEqual(terminal['stage'],'done')
        self.assertEqual(terminal['refresh']['status'],'refresh_failed')
        self.assertEqual(len(terminal['results']),1)

    def test_public_changed_file_uses_priority_lease_and_cuda(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        import threading
        calls=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append((self.path,body))
                result={'ok':True,'token':'synthetic-token'}
                self.send_response(200);self.end_headers();self.wfile.write(json.dumps(result).encode())
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        fake=self.root/'fake';fake.mkdir()
        (fake/'sentence_transformers.py').write_text("import numpy as np\nclass SentenceTransformer:\n def __init__(self,*args,device=None,**kw):\n  assert device=='cuda'\n def encode(self,texts,**kw):return np.ones((len(texts),384),dtype=np.float32)\n")
        self.path.write_text(self.new)
        env={**os.environ,'SEARCHFU_DB':str(self.db),'SEARCHFU_ANN_DIR':str(self.root/'index/ann'),
             'SEARCHFU_PY':os.sys.executable,'SEARCHFU_COLLECTIONS':str(self.config),
             'PYTHONPATH':str(fake),'SEARCHFU_ARCHON':f'http://127.0.0.1:{server.server_port}'}
        try:
            result=subprocess.run(['bash',str(Path(search.__file__).parent/'searchfu.sh'),'stream','violet','--fts','--refresh'],env=env,capture_output=True,text=True,check=True)
            events=[json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(events[-1]['refresh']['updated'],1)
            self.assertEqual(len(events[-1]['results']),1)
            self.assertEqual(calls[0],('/lease/acquire',{'ttl_secs':3600,'priority':True}))
            self.assertEqual(calls[-1][0],'/lease/release')
        finally:server.shutdown();server.server_close();thread.join()

    def test_shell_public_refresh_no_change(self):
        env={**os.environ,'SEARCHFU_DB':str(self.db),'SEARCHFU_ANN_DIR':str(self.root/'index/ann'),'SEARCHFU_PY':os.sys.executable,'SEARCHFU_COLLECTIONS':str(self.config)}
        result=subprocess.run(['bash',str(Path(search.__file__).parent/'searchfu.sh'),'stream','amber','--fts','--refresh'],env=env,capture_output=True,text=True,check=True)
        events=[json.loads(s) for s in result.stdout.splitlines()]
        self.assertEqual(events[-1]['refresh']['checked'],1)
        self.assertEqual(events[-1]['refresh']['updated'],0)
        for command in ['search','stream','start']:
            helptext=subprocess.run(['bash',str(Path(search.__file__).parent/'searchfu.sh'),command,'--help'],env=env,capture_output=True,text=True,check=True).stdout
            self.assertIn('--refresh',helptext)

if __name__=='__main__':unittest.main()
