#!/usr/bin/env python3
"""Ablation and combination experiments over generated data only."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
os.environ['CUDA_VISIBLE_DEVICES']=''
import numpy as np
from fixtures import fixture,FakeModels
from benchmark import recall,full_reference
from candidates import build
from retrieval import retrieve
from reranking import LocalReranker
import search


def median(values):return round(statistics.median(values),3)

def run(output,model_path,rows=30000):
    report={'kind':'synthetic_ablation_and_combinations','rows':rows,'files':3000,'vector_queries':5,
            'limitations':['Warm CPU synthetic data, not private corpus or cold disk.','Eight authored relevance cases are illustrative, not general relevance evaluation.',
                           'Vector recall compares to original vectors; relevance metrics use authored target documents.']}
    def no_walk(*args,**kwargs):raise AssertionError('source traversal')
    with tempfile.TemporaryDirectory(prefix='searchfu-experiments-') as t,patch('os.walk',no_walk):
        root=Path(t);db,ann,v=fixture(root,rows=rows,files=3000,block_rows=8192)
        with patch.dict(os.environ,{'SEARCHFU_ANN_DIR':str(ann)}):
            start=time.perf_counter();built=build(db,root/'candidates',max_rows=rows,threads=min(8,os.cpu_count() or 1))
            report['graph_build']={**built,'seconds':round(time.perf_counter()-start,3)}
            measurements={}
            for name,options in [('baseline',{}),('early_500',{'early':True,'candidate_ef':500}),('early_2000',{'early':True,'candidate_ef':2000})]:
                first=[];firstrec=[];useful=[];final=[];finalrec=[];payload=[];events_count=[]
                for q in v[np.linspace(0,rows-1,5,dtype=int)]:
                    expected=full_reference(v,q,3000,12)
                    events=list(retrieve(db,'no-keyword-match',models=FakeModels(q),emit_seconds=0,deep=True,**options))
                    initial=next(e for e in events if e['stage'] in ('candidates','semantic') and 'results' in e)
                    first.append(initial['elapsed_seconds']*1000);firstrec.append(recall([r['path'] for r in initial['results']],expected))
                    useful.append(next(e['elapsed_seconds']*1000 for e in events if 'results' in e and recall([r['path'] for r in e['results']],expected)>=0.8))
                    final.append(events[-1]['elapsed_seconds']*1000);finalrec.append(recall([r['path'] for r in events[-1]['results']],expected))
                    payload.append(len(json.dumps(events).encode()));events_count.append(len(events))
                measurements[name]={'first_semantic_median_ms':median(first),'first_file_recall_at_12':round(statistics.mean(firstrec),4),'time_to_80pct_recall_median_ms':median(useful),
                                    'deep_done_median_ms':median(final),'final_file_recall_at_12':round(statistics.mean(finalrec),4),
                                    'event_bytes_mean':round(statistics.mean(payload)),'events_mean':statistics.mean(events_count)}
            report['vector_ablation']=measurements
            q=v[-1]
            full=list(retrieve(db,'no-keyword-match',models=FakeModels(q),emit_seconds=0,deep=True,early=True))
            changes=list(retrieve(db,'no-keyword-match',models=FakeModels(q),emit_seconds=0,deep=True,early=True,deltas=True))
            current={}
            for event in changes:
                delta=event.get('changes')
                if delta:
                    for key in delta['removed']:current.pop(key,None)
                    for r in delta['added']+delta['updated']:current[r['result_id']]=r
                    result=[current[key] for key in delta['order']]
            assert result==full[-1]['results']
            report['delta_ablation']={'full_event_bytes':len(json.dumps(full).encode()),'delta_event_bytes':len(json.dumps(changes).encode()),
                                      'identical_reconstructed_results':True}
        cases=json.loads((Path(__file__).parent/'relevance-cases.json').read_text())
        texts=[]
        for case in cases:texts.extend([case['target']]+case['distractors'])
        texts.extend([f'This synthetic document is a weather report for location {i}. It records rainfall and temperature.' for i in range(80)])
        start=time.perf_counter();encoder=search.Models(False);vectors=encoder.text_vecs(texts)
        report['embedding_model_startup_and_corpus_encoding_ms']=round((time.perf_counter()-start)*1000,3)
        corpus=root/'relevance';corpus.mkdir();rdb,rann,_=fixture(corpus,rows=len(texts),files=len(texts))
        with closing(search.connect(rdb)) as c:
            for i,(text,vector) in enumerate(zip(texts,vectors),1):c.execute('UPDATE chunks SET text=?,embedding=? WHERE id=?',(text,search.pack(vector),i))
            c.commit()
        # Use a full-precision matrix for independent relevance experiments.
        meta=json.loads((rann/'ann.meta.json').read_text());meta['dtype']='fp16'
        for bi,lo in enumerate(range(0,len(vectors),128)):np.save(rann/'blocks'/f'emb-{bi:04d}.npy',vectors[lo:lo+128].astype(np.float16))
        (rann/'ann.meta.json').write_text(json.dumps(meta));build(rdb,corpus/'candidates',threads=2)
        scorer=LocalReranker(str(model_path));start=time.perf_counter();scorer.score([('synthetic question','synthetic passage')])
        report['reranker_model']=model_path.name
        report['reranker_startup_and_warmup_ms']=round((time.perf_counter()-start)*1000,3)
        combinations={}
        with patch.dict(os.environ,{'SEARCHFU_ANN_DIR':str(rann)}):
            for typed in [False,True]:
                for early in [False,True]:
                    for rerank in [False,True]:
                        key=f'typed={typed},early={early},rerank={rerank}'
                        ranks=[];done=[];first_hit=[];early_hit=[];bytes_sent=[]
                        for i,case in enumerate(cases):
                            gold=f'/synthetic-corpus/docs/item-{i*4}.txt'
                            opts={'models':encoder,'early':early,'emit_seconds':0,'top_k':3,'rerank_limit':24}
                            if typed:opts.update(lexical_queries=[case['tags']],semantic_queries=[case['question']])
                            if rerank:opts['reranker']=scorer
                            events=list(retrieve(rdb,case['tags'],**opts))
                            results=events[-1]['results'];paths=[r['path'] for r in results]
                            rank=paths.index(gold)+1 if gold in paths else None;ranks.append(rank)
                            done.append(events[-1]['elapsed_seconds']*1000);bytes_sent.append(len(json.dumps(events).encode()))
                            hit=next((e['elapsed_seconds']*1000 for e in events if gold in [r['path'] for r in e.get('results',[])]),None)
                            first_hit.append(hit)
                            early_event=next(e for e in events if e['stage'] in ('candidates','semantic') and 'results' in e)
                            early_hit.append(gold in [r['path'] for r in early_event['results']])
                        combinations[key]={'target_top1':sum(r==1 for r in ranks),'target_top3':sum(r is not None for r in ranks),'cases':len(cases),
                                           'mrr_at_3':round(sum(1/r if r else 0 for r in ranks)/len(cases),4),
                                           'done_median_ms':median(done),'first_target_median_ms':median([x for x in first_hit if x is not None]),
                                           'first_semantic_target_top3':sum(early_hit),'event_bytes_mean':round(statistics.mean(bytes_sent)),
                                           'target_ranks':ranks}
        report['relevance_combinations']=combinations
        blends={}
        with patch.dict(os.environ,{'SEARCHFU_ANN_DIR':str(rann)}):
            for mix in [0.25,0.5,0.75]:
                ranks=[];times=[]
                for i,case in enumerate(cases):
                    events=list(retrieve(rdb,case['tags'],lexical_queries=[case['tags']],semantic_queries=[case['question']],models=encoder,
                                         reranker=scorer,rerank_limit=24,rerank_mix=mix,top_k=3))
                    paths=[r['path'] for r in events[-1]['results']];gold=f'/synthetic-corpus/docs/item-{i*4}.txt'
                    ranks.append(paths.index(gold)+1 if gold in paths else None);times.append(events[-1]['elapsed_seconds']*1000)
                blends[str(mix)]={'target_top1':sum(r==1 for r in ranks),'target_top3':sum(r is not None for r in ranks),
                                  'mrr_at_3':round(sum(1/r if r else 0 for r in ranks)/len(cases),4),'done_median_ms':median(times),'target_ranks':ranks}
        report['reranker_blending']=blends
    report['source_tree_walked']=False
    output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'experiments_complete':True,'combinations':len(combinations),'source_tree_walked':False}))

if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--reranker',type=Path,required=True);ap.add_argument('--rows',type=int,default=30000)
    a=ap.parse_args();run(a.output,a.reranker,a.rows)
