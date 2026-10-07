"""Optional offline CPU passage reranking. Never opens source documents."""
import math

class LocalReranker:
    def __init__(self,model): self.model_name=model;self.model=None
    def score(self,pairs):
        if self.model is None:
            from sentence_transformers import CrossEncoder
            self.model=CrossEncoder(self.model_name,device='cpu',local_files_only=True,trust_remote_code=False,max_length=512)
        return self.model.predict(pairs,batch_size=8,show_progress_bar=False)


def rerank(c,lexical,semantic,query,top_k,limit,model,check,mix=1.0,*,where=(),params=(),collections=None):
    """Consider multiple retrieved passages per file, not only its fused snippet."""
    from retrieval import _results
    results=_results(c,lexical,semantic,limit,where=where,params=params,collections=collections)
    rows={}
    for best in lexical+semantic:
        for row in best.values(): rows.setdefault(row[1],{})[row[0]]=row
    pairs=[];locations=[]
    for result in results:
        for cid in sorted((x for x in rows.get(result.get('evidence_path',result['path']),{}) if x is not None))[:4]:
            check();text=c.execute('SELECT text FROM chunks WHERE id=?',(cid,)).fetchone()
            if text: pairs.append((query,text[0]));locations.append((result['path'],cid,text[0]))
    ranked={};scored=0
    for lo in range(0,len(pairs),8):
        check();scores=model.score(pairs[lo:lo+8]);check()
        if len(scores)!=len(pairs[lo:lo+8]): raise ValueError('invalid_reranker_scores')
        for location,score in zip(locations[lo:lo+8],scores):
            score=float(score)
            if not math.isfinite(score): raise ValueError('invalid_reranker_scores')
            path,cid,text=location
            if path not in ranked or score>ranked[path][0]: ranked[path]=(score,cid,text)
        scored+=len(scores)
    bypath={r['path']:r for r in results}
    base_ranks={r['path']:i+1 for i,r in enumerate(results)}
    learned_order=sorted(ranked,key=lambda p:(-ranked[p][0],p))
    learned_ranks={path:i+1 for i,path in enumerate(learned_order)}
    combined={path:(1-mix)/(60+base_ranks[path])+mix/(60+learned_ranks[path]) for path in ranked}
    out=[]
    for path in sorted(ranked,key=lambda p:(-combined[p],p))[:top_k]:
        score,cid,text=ranked[path]
        out.append({**bypath[path],'chunk_id':cid,'snippet':text,'relevance_score':score,'ranking_score':round(combined[path],8)})
    return out,scored
