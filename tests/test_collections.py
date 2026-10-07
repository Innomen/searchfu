import contextlib,io,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import collections_config as config
import search
from retrieval import retrieve
from fixtures import FakeModels

SOURCE=Path(__file__).resolve().parents[1]
class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.home=self.root/'nvme';self.ems=self.home/'ems';self.archive=self.root/'archive'
        self.ems.mkdir(parents=True);self.archive.mkdir()
        self.db=self.root/'runtime/index.sqlite3';self.cfg=self.root/'collections.json'
        self.env=patch.dict(os.environ,{'SEARCHFU_COLLECTIONS':str(self.cfg),'SEARCHFU_ANN_DIR':str(self.root/'ann'),'SEARCHFU_RESERVE_BYTES':str(1024**3),'SEARCHFU_CUDA':'0'})
        self.env.start()
        config.save({'version':1,'collections':{'ems':{'root':str(self.ems),'priority':10},'nvme':{'root':str(self.home),'priority':20},'archive':{'root':str(self.archive),'priority':30}}})
        for p in (self.ems/'target.txt',self.archive/'backup.txt'):p.write_text('Synthetic target cancellation preserves previously delivered search evidence. '*3)
        (self.archive/'old.txt').write_text('Synthetic target cancellation deletes previously delivered search evidence. '*3)
        (self.home/'other.bin').write_bytes(b'not text')
        with patch.object(search,'Models',return_value=FakeModels(__import__("numpy").eye(1,384,dtype="float32")[0])),contextlib.redirect_stdout(io.StringIO()):search.build(self.db,[str(self.ems),str(self.home),str(self.archive)],images=False)
    def tearDown(self):self.env.stop();self.tmp.cleanup()
    def run_search(self,**kw):
        with patch('os.walk',side_effect=AssertionError('search walked source')),patch('os.scandir',side_effect=AssertionError('search scanned source')):
            return list(retrieve(self.db,'target',fts_only=True,**kw))[-1]['results']
    def test_nested_views_do_not_repeat_index_rows(self):
        c=search.connect(self.db,readonly=True)
        try:self.assertEqual(c.execute('SELECT count(*) FROM files WHERE path=?',(str(self.ems/'target.txt'),)).fetchone()[0],1)
        finally:c.close()
        self.assertEqual(len(self.run_search(scopes=['ems'])),1)
        self.assertEqual(len(self.run_search(scopes=['nvme'])),1)
    def test_copy_union_prefers_working_file_and_preserves_old_version(self):
        results=self.run_search(scopes=['ems','archive']);self.assertEqual(len(results),2)
        current=next(r for r in results if len(r['copies'])==2)
        self.assertEqual(current['path'],str(self.ems/'target.txt'))
        self.assertEqual({x['path'] for x in current['copies']},{str(self.ems/'target.txt'),str(self.archive/'backup.txt')})
    def test_scope_does_not_reveal_other_collection_copies(self):
        results=self.run_search(scopes=['ems']);self.assertEqual(len(results[0]['copies']),1)
        self.assertNotIn(str(self.archive),json.dumps(results))
    def test_literal_scope_boundary_and_unknown_scope(self):
        sibling=self.home/'ems-extra';sibling.mkdir();(sibling/'target.txt').write_text('Synthetic target sibling should remain outside the ems collection. '*3)
        with patch.object(search,'Models',return_value=FakeModels(__import__("numpy").eye(1,384,dtype="float32")[0])),contextlib.redirect_stdout(io.StringIO()):search.build(self.db,[str(self.home)],images=False)
        self.assertEqual(len(self.run_search(scopes=['ems'])),1)
        with self.assertRaises(ValueError):self.run_search(scopes=['missing'])
    def test_catalog_finds_nontext_without_models(self):
        with patch.object(search.Models,'text_vecs',side_effect=AssertionError('loaded model')):
            events=list(retrieve(self.db,'other.bin',names_only=True,scopes=['nvme']))
        self.assertEqual(events[-1]['coverage'],'filename_catalog_only');self.assertEqual(events[-1]['results'][0]['snippet'],None)
    def test_offline_scope_refuses_index_and_query(self):
        data=config.load();data['collections']['archive']['searchable']=False;config.save(data)
        with self.assertRaises(ValueError):self.run_search(scopes=['archive'])
        p=subprocess.run(['bash',str(SOURCE/'searchfu.sh'),'scope','update','archive'],env={**os.environ,'SEARCHFU_DB':str(self.db)},capture_output=True,text=True)
        self.assertNotEqual(p.returncode,0)
    def test_public_routes_and_background_scope(self):
        env={**os.environ,'SEARCHFU_DB':str(self.db),'SEARCHFU_JOBS_DIR':str(self.root/'jobs')}
        for cmd in ('search','stream'):
            result=subprocess.run(['bash',str(SOURCE/'searchfu.sh'),cmd,'target','--scope','ems','--fts'],env=env,capture_output=True,text=True,check=True)
            self.assertNotIn(str(self.archive),result.stdout)
        started=json.loads(subprocess.run(['bash',str(SOURCE/'searchfu.sh'),'start','target','--scope','ems','--fts'],env=env,capture_output=True,text=True,check=True).stdout)
        import time
        for _ in range(100):
            polled=json.loads(subprocess.run(['bash',str(SOURCE/'searchfu.sh'),'poll',started['job_id']],env=env,capture_output=True,text=True,check=True).stdout)
            if polled['status'] in ('done','failed','cancelled'):break
            time.sleep(.02)
        self.assertEqual(polled['status'],'done');self.assertNotIn(str(self.archive),json.dumps(polled))
    def test_one_filesystem_excludes_other_stored_device(self):
        data=config.load();data['collections']['nvme'].update(one_filesystem=True,device=123);config.save(data)
        c=search.connect(self.db)
        with c:c.execute('UPDATE files SET source_device=456')
        c.close();self.assertEqual(self.run_search(scopes=['nvme']),[])
    def test_reader_change_cannot_publish_stale_hash(self):
        p=self.ems/'target.txt';st=p.stat();p.write_text(p.read_text()+'changed')
        with self.assertRaises(ValueError):search._file_items(self.ems,p,'text',st,25000)

    def test_scoped_health_is_aggregate_only(self):
        p=subprocess.run(['bash',str(SOURCE/'searchfu.sh'),'status','--agent','--scope','ems'],env={**os.environ,'SEARCHFU_DB':str(self.db)},capture_output=True,text=True,check=True)
        data=json.loads(p.stdout);self.assertEqual(data['files'],1);self.assertEqual(data['content_files'],1)
        self.assertNotIn(str(self.ems),p.stdout);self.assertNotIn(str(self.archive),p.stdout)
    def test_legacy_vision_search_obeys_scope(self):
        import numpy as np
        vector=np.eye(1,384,dtype=np.float32)[0];c=search.connect(self.db)
        with c:
            for root in (self.ems,self.archive):
                c.execute('INSERT INTO files(path,root,kind,mime,size,mtime,image_embedding) VALUES(?,?,?,?,?,?,?)',(str(root/'image.jpg'),str(root),'image','image/jpeg',1,1,search.pack(vector)))
        c.close();model=FakeModels(vector);model.clip_text=lambda query:vector
        results=search.search(self.db,'target',kind='image',images=True,models=model,scopes=['ems'])
        self.assertEqual([r['path'] for r in results],[str(self.ems/'image.jpg')])
