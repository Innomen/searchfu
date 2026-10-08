import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import search
import jobs
import ann as builder
from retrieval import retrieve,SearchCancelled,_matrix_pass,filters
from fixtures import fixture,FakeModels

SOURCE=Path(__file__).resolve().parents[1]

class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.db,self.ann,self.v=fixture(self.root,tail=3)
        self.env=patch.dict(os.environ,{'SEARCHFU_ANN_DIR':str(self.ann),'SEARCHFU_JOBS_DIR':str(self.root/'jobs'),'SEARCHFU_RESERVE_BYTES':str(1024**3)})
        self.env.start()
    def tearDown(self): self.env.stop();self.temp.cleanup()
    def run_search(self,**kw):
        with patch('os.walk',side_effect=AssertionError('search tried source traversal')), patch('os.scandir',side_effect=AssertionError('search tried directory enumeration')):
            return list(retrieve(self.db,'Topic1',models=FakeModels(self.v[0]),emit_seconds=0,**kw))
    def test_cancel_after_early_candidates_retains_evidence(self):
        try: import hnswlib
        except ImportError:self.skipTest('optional hnswlib not installed')
        from candidates import build
        build(self.db,self.root/'candidates',threads=2)
        stopped=False
        gen=retrieve(self.db,'unmatched',models=FakeModels(self.v[0]),early=True,cancelled=lambda:stopped)
        for event in gen:
            if event['stage']=='candidates':break
        stopped=True;last=next(gen);gen.close()
        self.assertEqual(last['stage'],'cancelled');self.assertEqual(last['results'],event['results'])
    def test_malformed_candidate_cache_is_skipped(self):
        directory=self.root/'candidates';directory.mkdir();(directory/'candidates.meta.json').write_text('[]')
        events=self.run_search(early=True)
        self.assertTrue(any(e.get('reason')=='candidate_cache_unusable' for e in events))
        self.assertEqual(events[-1]['stage'],'done')
    def test_candidate_build_cap_preserves_previous_cache(self):
        try: import hnswlib
        except ImportError:self.skipTest('optional hnswlib not installed')
        from candidates import build
        directory=self.root/'candidates';build(self.db,directory,threads=2)
        before=(directory/'candidates.meta.json').read_bytes()
        with self.assertRaisesRegex(ValueError,'candidate_row_limit'):build(self.db,directory,max_rows=1)
        self.assertEqual((directory/'candidates.meta.json').read_bytes(),before)
    def test_rerank_blend_zero_keeps_retrieval_order(self):
        class Reranker:
            def score(inner,pairs):return [100 if 'document 7.' in text else 0 for q,text in pairs]
        before=self.run_search()[-1]['results']
        after=self.run_search(reranker=Reranker(),rerank_mix=0)[-1]['results']
        self.assertEqual([r['path'] for r in before],[r['path'] for r in after])
    def test_reranker_cancel_keeps_previous_results(self):
        from retrieval import SearchCancelled
        class Reranker:
            def score(inner,pairs):raise SearchCancelled('synthetic stop')
        events=self.run_search(reranker=Reranker())
        self.assertEqual(events[-1]['stage'],'cancelled')
        self.assertEqual(events[-1]['results'],next(e['results'] for e in reversed(events[:-1]) if 'results' in e))
    def test_docx_ingest_and_provenance(self):
        import zipfile
        from extraction import extract
        source=self.root/'sources';source.mkdir();path=source/'sample.docx'
        text='This is fabricated document evidence about database indexing and safe stored search.'
        with zipfile.ZipFile(path,'w') as z:
            z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'+text+'</w:t></w:r></w:p></w:body></w:document>')
        self.assertEqual(extract(path),(text,'docx-text-v1'))
        db=self.root/'document.sqlite3'
        with patch.object(search,'Models',return_value=FakeModels(self.v[0])), patch.dict(os.environ,{'SEARCHFU_RESERVE_BYTES':str(1024**3)}),contextlib.redirect_stdout(io.StringIO()):
            search.build(db,[str(source)],images=False)
        c=search.connect(db,readonly=True)
        self.assertEqual(c.execute('SELECT extractor FROM files').fetchone()[0],'docx-text-v1');c.close()
        self.assertTrue(list(retrieve(db,'database',fts_only=True))[-1]['results'])
    def test_docx_rejects_entity_declarations(self):
        import zipfile
        from extraction import extract
        path=self.root/'unsafe.docx'
        with zipfile.ZipFile(path,'w') as z:z.writestr('word/document.xml','<!DOCTYPE document [<!ENTITY x "bad">]><document>&x;</document>')
        with self.assertRaisesRegex(ValueError,'document_xml_entities'):extract(path)
    def test_pdf_ingest_extracts_generated_text(self):
        try: import pypdf
        except ImportError:self.skipTest('optional pypdf not installed')
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
        from extraction import extract
        path=self.root/'sample.pdf';writer=PdfWriter();page=writer.add_blank_page(width=300,height=300)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        stream=DecodedStreamObject();stream.set_data(b'BT /F1 12 Tf 30 200 Td (Synthetic PDF evidence for indexed search.) Tj ET')
        page[NameObject('/Contents')]=writer._add_object(stream)
        with path.open('wb') as f:writer.write(f)
        text,method=extract(path);self.assertIn('Synthetic PDF evidence',text);self.assertEqual(method,'pdf-text-v1')
    def test_typed_query_routing(self):
        class Encoder:
            def text_vecs(inner,texts):
                self.assertEqual(texts,['conceptual description'])
                return FakeModels(self.v[0]).text_vecs(texts)
        events=list(retrieve(self.db,'not in corpus',lexical_queries=['Topic1'],semantic_queries=['conceptual description'],models=Encoder()))
        self.assertTrue(events[0]['results'])
        self.assertEqual(events[-1]['stage'],'done')
    def test_deltas_reconstruct_snapshots_and_cancel(self):
        plain=self.run_search(deep=True,batch_rows=30)
        delta=self.run_search(deep=True,batch_rows=30,deltas=True)
        current={}
        for expected,event in zip(plain,delta):
            changes=event.get('changes')
            if changes:
                for key in changes['removed']:current.pop(key,None)
                for r in changes['added']+changes['updated']:current[r['result_id']]=r
                self.assertEqual([current[k] for k in changes['order']],expected['results'])
        self.assertEqual(delta[-1]['results'],plain[-1]['results'])
        self.assertNotIn('results',delta[0])
    def test_reranker_changes_final_order_and_keeps_retrieval_coverage(self):
        class Reranker:
            def score(inner,pairs):return [100 if 'document 7.' in text else 0 for q,text in pairs]
        events=self.run_search(reranker=Reranker(),rerank_limit=50,deep=True)
        self.assertEqual(events[-1]['results'][0]['path'],'/synthetic-corpus/docs/item-7.txt')
        self.assertEqual(events[-1]['coverage'],'full_precision_indexed_vectors')
        self.assertIn('relevance_score',events[-1]['results'][0])
    def test_reranker_failure_preserves_evidence(self):
        class Broken:
            def score(inner,pairs):raise OSError('synthetic failure')
        events=self.run_search(reranker=Broken())
        self.assertTrue(any(e.get('reason')=='reranker_unavailable' for e in events))
        self.assertTrue(events[-1]['results'])
    def test_missing_early_cache_falls_back(self):
        events=self.run_search(early=True,deep=True)
        self.assertTrue(any(e.get('reason')=='candidate_cache_missing' for e in events))
        self.assertEqual(events[-1]['stage'],'done')
    def test_optional_candidate_graph_live_filter_and_cancel(self):
        try: import hnswlib
        except ImportError:self.skipTest('optional hnswlib not installed')
        from candidates import build
        directory=self.root/'candidates'
        with patch('os.walk',side_effect=AssertionError('source crawl')):
            build(self.db,directory,max_rows=1000,threads=2)
        with patch.dict(os.environ,{'SEARCHFU_CANDIDATES_DIR':str(directory)}):
            events=list(retrieve(self.db,'unmatched',models=FakeModels(self.v[0]),early=True,deep=True))
            early=next(e for e in events if e['stage']=='candidates')
            self.assertEqual(early['results'][0]['chunk_id'],1)
            c=search.connect(self.db);c.execute('DELETE FROM chunks WHERE id=1');c.commit();c.close()
            events=self.run_search(early=True,deep=True,path='missing-scope')
            self.assertTrue(all(not e.get('results') for e in events))
            gen=retrieve(self.db,'unmatched',models=FakeModels(self.v[0]),early=True)
            for event in gen:
                if event['stage']=='candidates':break
            gen.close()
    def test_candidate_build_front_door_and_wrong_identity_fallback(self):
        try: import hnswlib
        except ImportError:self.skipTest('optional hnswlib not installed')
        directory=self.root/'candidates'
        output=subprocess.check_output(['bash',str(SOURCE/'searchfu.sh'),'candidates-build','--threads','2'],env={**os.environ,'SEARCHFU_DB':str(self.db),'SEARCHFU_CANDIDATES_DIR':str(directory)},text=True)
        self.assertTrue(json.loads(output)['built'])
        pointer=directory/'candidates.meta.json';meta=json.loads(pointer.read_text());meta['index_id']='wrong';pointer.write_text(json.dumps(meta))
        with patch.dict(os.environ,{'SEARCHFU_CANDIDATES_DIR':str(directory)}):
            events=self.run_search(early=True)
        self.assertTrue(any(e.get('reason')=='candidate_cache_incompatible' for e in events))
        self.assertEqual(events[-1]['stage'],'done')
    def test_keyword_precedes_model_and_can_stop(self):
        model=FakeModels(self.v[0]);gen=retrieve(self.db,'Topic1',models=model)
        first=next(gen);self.assertEqual(first['stage'],'keyword');self.assertTrue(first['results'])
        self.assertEqual(model.calls,0);gen.close()
    def test_no_source_walk_and_progression(self):
        events=self.run_search(deep=True,batch_rows=30)
        stages=[x['stage'] for x in events]
        self.assertEqual(stages[0],'keyword');self.assertIn('refined',stages);self.assertIn('deep',stages)
        self.assertEqual(events[-1]['coverage'],'full_precision_indexed_vectors')
        self.assertTrue(all(not x['source_tree_walked'] for x in events))
        self.assertGreater(sum(x['stage']=='semantic' for x in events),1)
    def test_search_never_opens_or_stats_source_paths(self):
        import builtins
        real_open=builtins.open;real_stat=os.stat
        def guarded_open(path,*args,**kwargs):
            if isinstance(path,(str,os.PathLike)) and str(path).startswith('/synthetic-corpus'):
                raise AssertionError('search opened a source file')
            return real_open(path,*args,**kwargs)
        def guarded_stat(path,*args,**kwargs):
            if isinstance(path,(str,os.PathLike)) and str(path).startswith('/synthetic-corpus'):
                raise AssertionError('search inspected a source file')
            return real_stat(path,*args,**kwargs)
        with patch('builtins.open',side_effect=guarded_open), patch('os.stat',side_effect=guarded_stat):
            self.assertEqual(self.run_search(deep=True)[-1]['stage'],'done')
    def test_fts_does_not_import_or_load_model(self):
        with patch.object(search.Models,'text_vecs',side_effect=AssertionError('unexpected model')):
            events=list(retrieve(self.db,'Topic1',fts_only=True))
        self.assertEqual(events[-1]['coverage'],'keyword_index_only')
    def test_cancellation_preserves_keyword_results(self):
        stop=False;gen=retrieve(self.db,'Topic1',models=FakeModels(self.v[0]),cancelled=lambda:stop)
        first=next(gen);stop=True;last=next(gen)
        self.assertEqual(last['stage'],'cancelled');self.assertEqual(last['results'],first['results'])
        gen.close()
    def test_budget_preserves_results(self):
        gen=retrieve(self.db,'Topic1',models=FakeModels(self.v[0]),max_seconds=.1)
        first=next(gen);time.sleep(.11);last=next(gen)
        self.assertEqual(last['reason'],'budget_exhausted');self.assertEqual(last['results'],first['results']);gen.close()
    def test_missing_matrix_uses_only_stored_vectors(self):
        (self.ann/'ann.meta.json').unlink()
        events=self.run_search()
        self.assertEqual(events[-1]['coverage'],'full_precision_indexed_vectors')
    def test_deleted_high_rank_rows_and_tail(self):
        c=search.connect(self.db);c.execute('DELETE FROM chunks WHERE id=1');c.commit();c.close()
        events=list(retrieve(self.db,'unmatched',models=FakeModels(self.v[-1]),deep=True,emit_seconds=0))
        self.assertEqual(events[-1]['results'][0]['chunk_id'],len(self.v))
        self.assertNotIn(1,[r['chunk_id'] for e in events for r in e.get('results',[])])
    def test_relative_path_and_literal_wildcards(self):
        events=self.run_search(path='docs')
        self.assertTrue(events[-1]['results'])
        events=self.run_search(path='doc_')
        self.assertFalse(events[-1]['results'])
    def test_require_all_applies_only_to_keyword_matching(self):
        events=list(retrieve(self.db,'Topic1 absentword',fts_only=True,require_all=True))
        self.assertEqual(events[-1]['results'],[])
    def test_matrix_identity_mismatch_fails_after_keyword(self):
        meta=json.loads((self.ann/'ann.meta.json').read_text());meta['index_id']='wrong'
        (self.ann/'ann.meta.json').write_text(json.dumps(meta))
        gen=retrieve(self.db,'Topic1',models=FakeModels(self.v[0]))
        self.assertEqual(next(gen)['stage'],'keyword');next(gen)
        with self.assertRaisesRegex(ValueError,'matrix_index_mismatch'):list(gen)
    def test_int4_fp16(self):
        for dtype in ['int4','fp16']:
            root=self.root/dtype;root.mkdir()
            db,a,v=fixture(root,dtype=dtype)
            with patch.dict(os.environ,{'SEARCHFU_ANN_DIR':str(a)}):
                final=list(retrieve(db,'unmatched',models=FakeModels(v[0]),deep=True))[-1]
                self.assertEqual(final['results'][0]['chunk_id'],1)
    def test_query_guidance_and_expansion(self):
        query='I remember the thing that happened when we were discussing Topic1 and synthetic evidence for research'
        events=list(retrieve(self.db,query,fts_only=True,expansions=['Topic2']))
        self.assertTrue(events[0]['query_guidance']);self.assertEqual(events[0]['query_variants'],2)
    def test_status_cli_live_chunk_count(self):
        c=search.connect(self.db);c.execute('DELETE FROM chunks WHERE id=1');c.commit();c.close()
        output=subprocess.check_output(['bash',str(SOURCE/'searchfu.sh'),'status','--agent'],env={**os.environ,'SEARCHFU_DB':str(self.db),'SEARCHFU_DIR':str(self.root)},text=True)
        status=json.loads(output);self.assertEqual(status['chunks'],199);self.assertEqual(status['chunk_id_upper_bound'],200)
        self.assertNotIn('db',status)
    def test_shell_stream_front_door(self):
        output=subprocess.check_output(['bash',str(SOURCE/'searchfu.sh'),'stream','Topic1','--fts'],env={**os.environ,'SEARCHFU_DB':str(self.db)},text=True)
        events=[json.loads(line) for line in output.splitlines()]
        self.assertEqual([e['stage'] for e in events],['keyword','refresh','done'])
    def test_background_poll_cursor_and_private_files(self):
        args=type('Args',(),dict(query='Topic1',top_k=12,kind='all',after=None,before=None,path=None,fts_only=True,require_all=False,expansions=[],deep=False,max_seconds=None,batch_rows=8192,emit_seconds=1))()
        result=jobs.start(self.db,args);jid=result['job_id']
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            result=jobs.poll(jid)
            if result['status'] in jobs.TERMINAL:break
            time.sleep(.05)
        self.assertEqual(result['status'],'done')
        events=result['events'];self.assertEqual(events[-1]['stage'],'done')
        self.assertEqual(jobs.poll(jid,events[-1]['sequence'])['events'],[])
        directory=jobs._job(jid)
        self.assertEqual(directory.stat().st_mode&0o777,0o700)
        self.assertEqual((directory/'events.ndjson').stat().st_mode&0o777,0o600)
        self.assertFalse((directory/'request.json').exists())
        self.assertEqual(jobs.cancel(jid)['status'],'done')
    def test_prelaunch_cancellation(self):
        directory=self.root/'jobs'/('a'*32)
        directory.mkdir(parents=True,mode=0o700)
        jobs._write(directory/'request.json',{'db':str(self.db),'query':'Topic1','options':{'fts_only':True}})
        (directory/'cancel').touch()
        jobs.worker(directory)
        self.assertEqual(jobs.poll('a'*32)['status'],'cancelled')
    def test_job_path_rejects_traversal(self):
        with self.assertRaises(ValueError):jobs.poll('../escape')
    def test_cancel_while_waiting_for_matrix_lock(self):
        import fcntl
        from retrieval import _matrix_lock
        lock=(self.ann/'.build.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX)
        checks=0
        def check():
            nonlocal checks
            checks+=1
            if checks>2:raise SearchCancelled('cancelled')
        try:
            with self.assertRaises(SearchCancelled):
                with _matrix_lock(self.ann,check):pass
        finally:lock.close()
    def test_dtype_change_refused_and_full_rebuild(self):
        attrs={'DB':self.db,'ANN':self.ann,'BLOCKS':self.ann/'blocks','PROGRESS':self.ann/'ann.progress.json','META':self.ann/'ann.meta.json','SCALE_FILE':self.ann/'ann.scale.json','DATA':self.root,'BLOCK_ROWS':64}
        with contextlib.ExitStack() as stack:
            for name,val in attrs.items():stack.enter_context(patch.object(builder,name,val))
            with self.assertRaisesRegex(ValueError,'dtype_change'):builder.run_build(False,dtype='int4')
            self.assertTrue(builder.META.exists())
            builder.run_build(False,full=True,dtype='int4')
            self.assertEqual(json.loads(builder.META.read_text())['dtype'],'int4')
    def test_failed_full_rebuild_invalidates_cache(self):
        attrs={'DB':self.db,'ANN':self.ann,'BLOCKS':self.ann/'blocks','PROGRESS':self.ann/'ann.progress.json','META':self.ann/'ann.meta.json','SCALE_FILE':self.ann/'ann.scale.json','DATA':self.root}
        with contextlib.ExitStack() as stack:
            for name,val in attrs.items():stack.enter_context(patch.object(builder,name,val))
            stack.enter_context(patch.object(builder,'compute_scale',side_effect=RuntimeError('injected')))
            with self.assertRaises(RuntimeError):builder.run_build(False,full=True)
        self.assertFalse((self.ann/'ann.meta.json').exists())
        self.assertEqual(self.run_search()[-1]['coverage'],'full_precision_indexed_vectors')
    def test_verifier_uses_float32_reference(self):
        attrs={'DB':self.db,'ANN':self.ann,'BLOCKS':self.ann/'blocks','DATA':self.root}
        output=io.StringIO()
        with contextlib.ExitStack() as stack:
            for name,val in attrs.items():stack.enter_context(patch.object(builder,name,val))
            with contextlib.redirect_stdout(output):builder.run_verify(n=3)
        report=json.loads(output.getvalue())
        self.assertEqual(report['reference'],'stored_float32_vectors');self.assertGreater(report['recall_at_30_mean'],.9)

class PublicOptionParity(unittest.TestCase):
    def test_all_search_frontdoors_expose_optional_stages(self):
        for command in ('search','stream','start'):
            output=subprocess.run([sys.executable,str(SOURCE/'search.py'),command,'--help'],capture_output=True,text=True,check=True).stdout
            for option in ('--lex','--semantic','--early','--candidate-ef','--rerank-model','--rerank-mix','--rerank-early','--deltas','--deep'):
                self.assertIn(option,output)
    def test_blocking_wrapper_forwards_options_and_terminal_snapshot(self):
        expected=[{'result_id':'synthetic','score':1}]
        with patch('retrieval.retrieve',return_value=iter([{'stage':'done','results':expected}])) as engine:
            self.assertEqual(search.search(Path('/synthetic/index.db'),'tags',images=False,semantic_queries=['concept'],early=True,deltas=True),expected)
        self.assertEqual(engine.call_args.kwargs['semantic_queries'],['concept'])
        self.assertTrue(engine.call_args.kwargs['early'])
        self.assertTrue(engine.call_args.kwargs['deltas'])

if __name__=='__main__':unittest.main()
