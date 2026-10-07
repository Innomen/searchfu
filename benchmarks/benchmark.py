#!/usr/bin/env python3
"""Reproducible synthetic benchmarks. Never indexes or searches personal data.

Vector timing injects a deterministic query encoder to isolate retrieval cost.
Optional cached MiniLM timings separately measure real query encoding. HNSW is
an optional comparison of vector backends, not a QMD/LEANN product benchmark.
"""
import argparse
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import statistics
import sys
import tarfile
import tempfile
import time
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
os.environ["CUDA_VISIBLE_DEVICES"]=""
import numpy as np
from fixtures import fixture,FakeModels
from retrieval import retrieve,_matrix_pass,_ranked_rows
import search


def elapsed(fn):
    start=time.perf_counter();value=fn();return time.perf_counter()-start,value


def recall(paths,reference):
    return len(set(paths)&set(reference))/len(reference)


def full_reference(v,q,files,k):
    scores=v@q;best={}
    for i,score in enumerate(scores):
        fid=i%files
        best[fid]=max(best.get(fid,-1),float(score))
    return [f'/synthetic-corpus/docs/item-{i}.txt' for i in sorted(best,key=best.get,reverse=True)[:k]]


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--rows',type=int,default=30000);ap.add_argument('--files',type=int,default=3000)
    ap.add_argument('--queries',type=int,default=5);ap.add_argument('--legacy-backup',type=Path)
    ap.add_argument('--output',type=Path);ap.add_argument('--skip-real-model',action='store_true')
    a=ap.parse_args()
    if a.rows<40 or a.files<1 or a.queries<1:ap.error('positive sizes required; rows >= 40')
    report={'kind':'synthetic_warm_cache','rows':a.rows,'files':a.files,'dimensions':384,
            'queries':a.queries,'cpu_only':True,'encoder':'injected_normalized_vectors','threads':os.environ.get('OPENBLAS_NUM_THREADS','unspecified'),
            'limitations':['Not a large-drive or cold-cache benchmark.','Random vectors are not human relevance judgments.',
                           'HNSW measures a backend, not the complete QMD or LEANN applications.']}
    with tempfile.TemporaryDirectory(prefix='searchfu-benchmark-') as temp:
        root=Path(temp);db,ann,v=fixture(root,rows=a.rows,files=a.files,block_rows=8192)
        queries=v[np.linspace(0,a.rows-1,a.queries,dtype=int)]
        original_walk=os.walk
        def no_walk(*args,**kwargs):raise AssertionError('search attempted a source crawl')
        with patch.dict(os.environ,{'SEARCHFU_ANN_DIR':str(ann)}),patch('os.walk',no_walk):
            tiers={}
            for dtype in ['int4','int8','fp16']:
                from ann import quantize
                scale=np.abs(v).max(axis=0)*1.1
                meta=json.loads((ann/'ann.meta.json').read_text());meta['dtype']=dtype
                for bi,lo in enumerate(range(0,a.rows,8192)):
                    np.save(ann/'blocks'/f'emb-{bi:04d}.npy',quantize(v[lo:lo+8192],scale,dtype))
                (ann/'ann.meta.json').write_text(json.dumps(meta))
                first_keyword=[];first_semantic=[];refined=[];complete=[];quantized_rec=[];refined_rec=[];deep_times=[]
                for q in queries:
                    reference=full_reference(v,q,a.files,12)
                    stages=list(retrieve(db,'unmatched-term',models=FakeModels(q),emit_seconds=0))
                    first_keyword.append(stages[0]['elapsed_seconds'])
                    first_semantic.append(next(e['elapsed_seconds'] for e in stages if e['stage']=='semantic'))
                    sem=next(e for e in reversed(stages) if e['stage']=='semantic')
                    ref=next(e for e in reversed(stages) if e['stage']=='refined')
                    quantized_rec.append(recall([r['path'] for r in sem['results']],reference))
                    refined_rec.append(recall([r['path'] for r in ref['results']],reference))
                    refined.append(ref['elapsed_seconds']);complete.append(stages[-1]['elapsed_seconds'])
                    seconds,deep=elapsed(lambda:list(retrieve(db,'unmatched-term',models=FakeModels(q),deep=True,emit_seconds=100)))
                    deep_times.append(seconds)
                    assert recall([r['path'] for r in deep[-1]['results']],reference)==1
                tiers[dtype]={'no_match_keyword_first_median_ms':round(statistics.median(first_keyword)*1000,3),
                    'semantic_first_median_ms':round(statistics.median(first_semantic)*1000,3),
                    'refined_done_median_ms':round(statistics.median(refined)*1000,3),
                    'deep_done_median_ms':round(statistics.median(deep_times)*1000,3),
                    'quantized_file_recall_at_12':round(statistics.mean(quantized_rec),4),
                    'refined_file_recall_at_12':round(statistics.mean(refined_rec),4),
                    'deep_file_recall_at_12':1.0,'cache_bytes':sum(p.stat().st_size for p in (ann/'blocks').glob('*.npy'))}
            report['progressive']=tiers
            short='Topic1 synthetic evidence'
            long='I remember discussing a research document and some evidence that happened in the synthetic context of Topic1 and I want to locate the distinctive sample from that conversation about a document and research tags'
            lexical={}
            for label,query in [('short_tags',short),('long_narrative',long)]:
                values=[]
                for _ in range(15):
                    seconds,events=elapsed(lambda:list(retrieve(db,query,fts_only=True)))
                    values.append(seconds*1000)
                lexical[label]={'words':len(query.split()),'median_ms':round(statistics.median(values),3)}
            report['keyword_query_length']=lexical
            stop=False;gen=retrieve(db,short,models=FakeModels(queries[0]),cancelled=lambda:stop)
            first=next(gen);stop=True
            seconds,cancelled=elapsed(lambda:next(gen));gen.close()
            assert cancelled['stage']=='cancelled' and cancelled['results']==first['results']
            report['cancel_after_keywords_ms']=round(seconds*1000,3)
            if a.legacy_backup:
                with tarfile.open(a.legacy_backup) as archive:
                    (root/'legacy_search.py').write_bytes(archive.extractfile('search.py').read())
                spec=importlib.util.spec_from_file_location('legacy_search',root/'legacy_search.py')
                legacy=importlib.util.module_from_spec(spec);spec.loader.exec_module(legacy)
                # Legacy's Torch import/thread setup is excluded from warm timings.
                import torch
                torch.set_num_threads(1)
                old=[]
                for q in queries:
                    seconds,_=elapsed(lambda:legacy.search(db,'unmatched-term',images=False,models=FakeModels(q)))
                    old.append(seconds*1000)
                report['legacy_blocking_warm_median_ms']=round(statistics.median(old),3)
            report['database_bytes']=db.stat().st_size
        # Optional existing HNSW dependency: no installs or model downloads.
        try:
            import hnswlib
            index=hnswlib.Index(space='cosine',dim=384)
            index.init_index(max_elements=a.rows,ef_construction=100,M=16,random_seed=42)
            seconds,_=elapsed(lambda:index.add_items(v,np.arange(a.rows),num_threads=1))
            hnsw={'build_seconds':round(seconds,3),'queries':{}}
            index.save_index(str(root/'hnsw.bin'));hnsw['index_bytes']=(root/'hnsw.bin').stat().st_size
            reloaded=hnswlib.Index(space='cosine',dim=384)
            load_seconds,_=elapsed(lambda:reloaded.load_index(str(root/'hnsw.bin')))
            hnsw['load_from_warm_disk_ms']=round(load_seconds*1000,3)
            for ef in [50,500,2000]:
                index.set_ef(ef);timings=[];rec=[]
                for q in queries:
                    reference=full_reference(v,q,a.files,12)
                    seconds,result=elapsed(lambda:index.knn_query(q,k=min(a.rows,180),num_threads=1))
                    labels,dist=result;seen=[]
                    for cid in labels[0]:
                        path=f'/synthetic-corpus/docs/item-{int(cid)%a.files}.txt'
                        if path not in seen:seen.append(path)
                        if len(seen)==12:break
                    timings.append(seconds*1000);rec.append(recall(seen,reference))
                hnsw['queries'][str(ef)]={'median_ms':round(statistics.median(timings),3),'file_recall_at_12':round(statistics.mean(rec),4)}
            report['hnsw_backend']=hnsw
        except ImportError:report['hnsw_backend']={'available':False}
        if not a.skip_real_model:
            try:
                model=search.Models(False)
                model.text_vecs(['synthetic warmup'])
                values={}
                for label,query in [('short_tags',short),('long_narrative',long)]:
                    times=[]
                    for _ in range(5):
                        seconds,_=elapsed(lambda:model.text_vecs([query]));times.append(seconds*1000)
                    values[label]=round(statistics.median(times),3)
                report['cached_minilm_encoding_median_ms']=values
            except Exception as exc:
                report['cached_minilm_encoding']={'available':False,'exception_class':type(exc).__name__}
    report['source_tree_walked']=False
    output=json.dumps(report,indent=2)+'\n'
    if a.output:a.output.write_text(output)
    print(output)

if __name__=='__main__':main()
