"""Progressive retrieval from an existing index. Never reads source files.

The only disk inputs are SQLite, matrix metadata and matrix blocks. All result
snippets come from SQLite. Source traversal belongs exclusively to indexing.
"""
from __future__ import annotations
from contextlib import closing, contextmanager
from pathlib import Path
import hashlib
import errno
import json
import os
import re
import time
import corpus_filter

class SearchCancelled(Exception):
    pass


def filters(kind="all", after=None, before=None, path=None):
    from search import _date
    where, params = [], []
    if kind != "all": where.append("f.kind=?"); params.append(kind)
    if after: where.append("f.mtime>=?"); params.append(_date(after))
    if before: where.append("f.mtime<=?"); params.append(_date(before, True))
    if path:
        # Literal prefix, either the stored path or relative to its indexed root.
        # SQL wildcards in filenames must not broaden the requested scope.
        prefix = path.rstrip("/")
        prefix = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"
        where.append("(f.path LIKE ? ESCAPE '\\' OR "
                     "CASE WHEN substr(f.path,1,length(rtrim(f.root,'/'))+1)=rtrim(f.root,'/')||'/' "
                     "THEN substr(f.path,length(rtrim(f.root,'/'))+2) ELSE f.path END LIKE ? ESCAPE '\\')")
        params.extend([prefix, prefix])
    return where, params


def _extra(where):
    return (" AND " + " AND ".join(where)) if where else ""


def _lexical(c, query, limit, where, params, require_all):
    terms = list(dict.fromkeys(re.findall(r"[\w-]+", query)))
    if not terms: return {}
    operator = " AND " if require_all else " OR "
    expression = operator.join('"' + t.replace('"', '""') + '"' for t in terms)
    sql = ("SELECT c.id,f.path,f.kind,f.mtime,f.mime,bm25(chunks_fts) "
           "FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid "
           "JOIN files f ON f.id=c.file_id WHERE chunks_fts MATCH ?" + _extra(where) +
           " ORDER BY bm25(chunks_fts) LIMIT ?")
    best = {}
    # Fetch in pages: long files must not consume every candidate slot.
    # The bound applies to files; OFFSET is only over the FTS index.
    offset = 0
    while len(best) < limit:
        rows = list(c.execute(sql + " OFFSET ?", [expression] + params + [max(300, limit*2), offset]))
        if not rows: break
        for cid,p,k,mt,mime,bm in rows:
            if not corpus_filter.is_junk(p) and p not in best:
                best[p] = (cid,p,k,mt,mime,-float(bm))
                if len(best) == limit: break
        offset += len(rows)
    return best


def _trim(best, limit):
    if len(best) > limit:
        return dict(sorted(best.items(), key=lambda item: (-item[1][-1], item[0]))[:limit])
    return best


def _merge(best, rows, limit):
    for row in rows:
        p = row[1]
        if p not in best or row[-1] > best[p][-1]: best[p] = row
    return _trim(best, limit)


def _ranked_rows(best):
    return sorted(best.values(), key=lambda r: (-r[-1],r[1]))


def _names(c,query,limit,where,params,require_all):
    terms=list(dict.fromkeys(re.findall(r"[\w.-]+",query)))[:32]
    if not terms:return {}
    # Literal substring lookup over stored paths; no glob expansion or source IO.
    op=' AND ' if require_all else ' OR '
    clause='('+op.join("instr(lower(f.path),lower(?))>0" for _ in terms)+')'
    sql='SELECT f.path,f.kind,f.mtime,f.mime FROM files f WHERE '+clause+_extra(where)+' ORDER BY f.path LIMIT ?'
    return {p:(None,p,k,mt,mime,1.0) for p,k,mt,mime in c.execute(sql,terms+params+[limit]) if not corpus_filter.is_junk(p)}


def _results(c, lexical, semantic, top_k, *, where=(), params=(), collections=None):
    """File rank fusion; identical indexed bytes share evidence and provenance."""
    from collections_config import memberships
    collections=collections or {}
    scores,chosen,ranks={},{},{}
    for source,best in [("keyword",x) for x in lexical]+[("semantic",x) for x in semantic]:
        for rank,row in enumerate(_ranked_rows(best),1):
            p=row[1];scores[p]=scores.get(p,0.0)+1.0/(60+rank)
            ranks.setdefault(p,{})[source]=min(rank,ranks.get(p,{}).get(source,rank))
            if p not in chosen or rank<chosen[p][0]:chosen[p]=(rank,row)
    modern='content_hash' in {r[1] for r in c.execute('PRAGMA table_info(files)')}
    identities={};out=[]
    for p in sorted(scores,key=lambda p:(-scores[p],p)):
        row=chosen[p][1]
        meta=c.execute('SELECT content_hash,source_device FROM files WHERE path=?',(p,)).fetchone() if modern else (None,None)
        digest,device=meta or (None,None)
        identity='bytes:'+digest if digest else 'path:'+p
        if identity in identities:continue
        identities[identity]=True
        text=c.execute('SELECT text FROM chunks WHERE id=?',(row[0],)).fetchone() if row[0] is not None else None
        if row[0] is not None and text is None:continue
        copies=[(p,row[3],device)]
        if digest:
            copies=list(c.execute('SELECT f.path,f.mtime,f.source_device FROM files f WHERE f.content_hash=?'+_extra(where)+' ORDER BY f.path',[digest]+list(params)))
        def preference(copy):
            names=memberships(copy[0],collections,copy[2])
            return (min((collections[n].get('priority',100) for n in names),default=100),-float(copy[1] or 0),copy[0])
        copies.sort(key=preference);canonical=copies[0][0] if copies else p
        out.append({'result_id':hashlib.sha256(identity.encode()).hexdigest(),'chunk_id':row[0],
                    'score':round(scores[p],8),'snippet':text[0] if text else None,
                    'path':canonical,'evidence_path':p,'kind':row[2],'mtime':copies[0][1] if copies else row[3],'mime':row[4],
                    'match':'filename' if row[0] is None else 'hybrid' if len(ranks[p])>1 else next(iter(ranks[p])),
                    'ranks':ranks[p], 'copies':[{'path':cp,'mtime':mt,'scopes':memberships(cp,collections,dev)} for cp,mt,dev in copies]})
        if len(out)>=top_k:break
    return out


@contextmanager
def _matrix_lock(ann, check):
    """Wait cooperatively for an index writer; never busy-loop or block cancel."""
    import fcntl
    # New indexes always contain this file. Legacy indexes can create only an
    # advisory lock, never mutate indexed content while searching.
    with open(ann / ".build.lock", "a") as lock:
        while True:
            check()
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES): raise
                time.sleep(.05)
        try: yield
        finally: fcntl.flock(lock, fcntl.LOCK_UN)


def _metadata(ann, c):
    meta = json.loads((ann / "ann.meta.json").read_text())
    if meta.get("dim",384) != 384 or meta.get("dtype","fp16") not in ("int4","int8","fp16"):
        raise ValueError("unsupported_matrix_format")
    identity = c.execute("SELECT value FROM index_state WHERE key='index_id'").fetchone()
    if meta.get("index_id") and (not identity or str(identity[0]) != str(meta["index_id"])):
        raise ValueError("matrix_index_mismatch")
    if meta.get("filter_version",1) != corpus_filter.FILTER_VERSION:
        raise ValueError("matrix_filter_mismatch")
    return meta


def _select(c, ids, scores, limit, where, params):
    """Widen per-batch candidates until enough eligible distinct files exist."""
    import numpy as np
    if not len(ids): return {}
    order = np.argsort(-scores, kind="stable")
    best, pos = {}, 0
    while pos < len(order) and len(best) < limit:
        ix = order[pos:pos+500]; pos += len(ix)
        values = {int(ids[i]):float(scores[i]) for i in ix}
        sql = ("SELECT c.id,f.path,f.kind,f.mtime,f.mime FROM chunks c "
               "JOIN files f ON f.id=c.file_id WHERE c.id IN ("+
               ",".join("?"*len(values))+")" + _extra(where))
        for cid,p,k,mt,mime in c.execute(sql,list(values)+params):
            if corpus_filter.is_junk(p): continue
            row=(cid,p,k,mt,mime,values[cid])
            if p not in best or row[-1]>best[p][-1]: best[p]=row
    return _trim(best,limit)


def _matrix_pass(c, ann, queries, limit, where, params, check, batch_rows, initial=None):
    import numpy as np
    from search import dequant_block
    meta = _metadata(ann,c)
    ids = np.load(ann/"blocks"/"ids.npy",mmap_mode="r",allow_pickle=False)
    if len(ids) != int(meta["total_kept"]): raise ValueError("matrix_row_mismatch")
    best = initial if initial is not None else [{} for _ in queries]
    offset, scored = 0, 0
    for bi in range(int(meta["n_blocks"])):
        check()
        block = np.load(ann/"blocks"/("emb-%04d.npy"%bi),mmap_mode="r",allow_pickle=False)
        expected = 192 if meta.get("dtype") == "int4" else 384
        if block.ndim!=2 or block.shape[1]!=expected or offset+len(block)>len(ids):
            raise ValueError("matrix_block_mismatch")
        for lo in range(0,len(block),batch_rows):
            check()
            hi=min(len(block),lo+batch_rows)
            vectors=dequant_block(block[lo:hi],meta)
            # Cosine on the quantized representation, not original-f32 exactness.
            norms=np.linalg.norm(vectors,axis=1)
            vectors=vectors/np.maximum(norms[:,None],1e-12)
            scores=vectors@queries.T
            batch_ids=ids[offset+lo:offset+hi]
            # A read snapshot predating a concurrent append must not score IDs
            # outside its canonical SQLite view (selection also drops dead IDs).
            for qi in range(len(queries)):
                local=_select(c,batch_ids,scores[:,qi],limit,where,params)
                best[qi]=_merge(best[qi],local.values(),limit)
            scored+=hi-lo
            yield best, {"matrix_rows_scored":scored,"matrix_rows_total":len(ids),
                         "representation":meta.get("dtype","fp16")}
        offset+=len(block)
    if offset!=len(ids): raise ValueError("matrix_row_mismatch")
    # New SQLite rows not yet incorporated into this matrix still participate.
    for best,progress in _full_pass(c,queries,limit,where,params,check,batch_rows,
                                   lower=int(meta.get("db_max_rowid",0)),initial=best):
        progress.update(matrix_rows_scored=scored,matrix_rows_total=len(ids),
                        representation=meta.get("dtype","fp16"))
        yield best,progress


def _scoped_ids(c, where, params, lower=0, limit=None):
    # Inventory first: do not walk millions of vector blobs to find a small scope.
    sql = ("SELECT c.id FROM files f CROSS JOIN chunks c INDEXED BY chunks_file "
           "ON c.file_id=f.id WHERE c.id>?" + _extra(where) + " ORDER BY c.id")
    values = [lower] + params
    if limit is not None:
        sql += " LIMIT ?"; values.append(limit)
    return c.execute(sql, values)


def _prefer_scoped_vectors(c, ann, where, params):
    if not where: return False
    total = int(_metadata(ann,c)["total_kept"])
    cutoff = min(1_000_000, total // 8)
    if cutoff < 1: return False
    # No vector reads; the bound also limits the temporary ID sort.
    return len(list(_scoped_ids(c,where,params,limit=cutoff+1))) <= cutoff


def _full_pass(c,queries,limit,where,params,check,batch_rows,lower=0,initial=None):
    import numpy as np
    best = initial if initial is not None else [{} for _ in queries]
    sql=("SELECT c.id,f.path,f.kind,f.mtime,f.mime,c.embedding FROM chunks c "
         "JOIN files f ON f.id=c.file_id WHERE c.id>?"+_extra(where)+" ORDER BY c.id")
    cursor = _scoped_ids(c,where,params,lower) if where else c.execute(sql,[lower]+params)
    scanned,scored,skipped=0,0,0
    while True:
        check()
        rows=cursor.fetchmany(min(batch_rows,500) if where else batch_rows)
        if not rows: break
        if where:
            ids=[r[0] for r in rows]
            rows=list(c.execute("SELECT c.id,f.path,f.kind,f.mtime,f.mime,c.embedding "
                                "FROM chunks c JOIN files f ON f.id=c.file_id WHERE c.id IN ("+
                                ",".join("?"*len(ids))+") ORDER BY c.id",ids))
        scanned+=len(rows)
        valid=[r for r in rows if not corpus_filter.is_junk(r[1]) and r[-1] is not None and len(r[-1])==1536]
        skipped+=len(rows)-len(valid)
        if valid:
            vectors=np.stack([np.frombuffer(r[-1],dtype='<f4') for r in valid])
            norms=np.linalg.norm(vectors,axis=1)
            vectors=vectors/np.maximum(norms[:,None],1e-12)
            scores=vectors@queries.T
            for qi in range(len(queries)):
                best[qi]=_merge(best[qi],(r[:-1]+(float(scores[i,qi]),) for i,r in enumerate(valid)),limit)
            scored+=len(valid)
        yield best,{"sqlite_rows_visited":scanned,"sqlite_vectors_scored":scored,"sqlite_rows_skipped":skipped}
    yield best,{"sqlite_rows_visited":scanned,"sqlite_vectors_scored":scored,"sqlite_rows_skipped":skipped}


def _refine(c,queries,semantic,lexical,limit,check):
    import numpy as np
    rows={r[0]:r for best in semantic+lexical for r in best.values()}
    exact=[{} for _ in queries]
    for lo in range(0,len(rows),500):
        check()
        ids=list(rows)[lo:lo+500]
        sql="SELECT id,embedding FROM chunks WHERE id IN ("+",".join("?"*len(ids))+")"
        for cid,blob in c.execute(sql,ids):
            if blob is None or len(blob)!=1536: continue
            v=np.frombuffer(blob,dtype='<f4');v=v/max(float(np.linalg.norm(v)),1e-12)
            scores=queries@v
            for qi in range(len(queries)):
                row=rows[cid][:-1]+(float(scores[qi]),)
                exact[qi]=_merge(exact[qi],[row],limit)
    return exact


def retrieve(db, query, *, top_k=12, kind="all", after=None, before=None,
             path=None, fts_only=False, require_all=False, models=None,
             expansions=(), deep=False, cancelled=None, max_seconds=None,
             batch_rows=8192, emit_seconds=1.0, lexical_queries=None, semantic_queries=None,
             early=False, candidate_ef=500, rerank_model=None, reranker=None,
             rerank_limit=36, rerank_mix=1.0, rerank_early=False, deltas=False, scopes=(), collections_file=None, names_only=False):
    """Yield NDJSON-ready snapshots. Results are cumulative evidence, rankings
    may change. Only a completed full-f32 stage is exhaustive at full precision.
    No absence-of-evidence or corpus-level completeness claim follows from it.
    """
    from search import connect,Models
    started=time.monotonic()
    deadline=started+max_seconds if max_seconds is not None else None
    def check():
        if cancelled and cancelled(): raise SearchCancelled("cancelled")
        if deadline is not None and time.monotonic()>=deadline: raise SearchCancelled("budget_exhausted")
    if top_k<1 or batch_rows<1 or emit_seconds<0: raise ValueError("invalid_search_limits")
    texts=list(dict.fromkeys([query]+list(expansions)))
    lexical_texts=list(dict.fromkeys(texts if lexical_queries is None else lexical_queries))
    semantic_texts=list(dict.fromkeys(texts if semantic_queries is None else semantic_queries))
    if not lexical_texts or not semantic_texts or max(len(texts),len(lexical_texts),len(semantic_texts))>8:
        raise ValueError("invalid_query_variants")
    if not 0<=rerank_mix<=1: raise ValueError("invalid_rerank_mix")
    if candidate_ef<1 or rerank_limit<1 or rerank_limit>200 or ((rerank_model or reranker) and rerank_limit<top_k): raise ValueError("invalid_stage_limits")
    where,params=filters(kind,after,before,path)
    from collections_config import scope_sql
    scope_where,scope_params,selected=scope_sql(scopes,collections_file)
    where+=scope_where;params+=scope_params
    pool=max(150,top_k*15)
    last_results=[];stage="keyword";last_progress={}
    previous={}
    def event(stage,results=None,**fields):
        nonlocal previous
        out={"stage":stage,"elapsed_seconds":round(time.monotonic()-started,4),
             "source_tree_walked":False,"scopes":list(selected),**fields}
        if results is not None:
            current={r['result_id']:r for r in results}
            if deltas:
                out['changes']={'added':[r for key,r in current.items() if key not in previous],
                                'updated':[r for key,r in current.items() if key in previous and r!=previous[key]],
                                'removed':[key for key in previous if key not in current],
                                'order':list(current)}
            if not deltas or stage in ('done','cancelled'): out["results"]=results
            previous=current
        return out
    try:
        with closing(connect(Path(db),readonly=True)) as c:
            c.execute("BEGIN")
            c.set_progress_handler(lambda: 1 if (cancelled and cancelled()) or (deadline is not None and time.monotonic()>=deadline) else 0,10000)
            check()
            def results(lexical,semantic): return _results(c,lexical,semantic,top_k,where=where,params=params,collections=selected)
            lexical=[(_names if names_only else _lexical)(c,t,pool,where,params,require_all) for t in lexical_texts]
            last_results=results(lexical,[])
            state=dict(c.execute("SELECT key,value FROM index_state WHERE key LIKE 'last_build_%'"))
            snapshot={"update_incomplete":state.get("last_build_started",0)>state.get("last_build_finished",0),
                      "last_build_errors":state.get("last_build_errors",0),
                      "last_build_walk_errors":state.get("last_build_walk_errors",0)}
            yield event("keyword",last_results,complete=False,query_variants=len(texts),lexical_variants=len(lexical_texts),semantic_variants=len(semantic_texts),index_health=snapshot,
                        query_guidance="Use 3-8 distinctive terms instead of a narrative; --expand adds another angle." if any(len(re.findall(r"[\w-]+",t))>12 for t in lexical_texts) else None)
            check()
            if fts_only or names_only:
                yield event("done",last_results,complete=True,coverage="filename_catalog_only" if names_only else "keyword_index_only",index_health=snapshot)
                return
            stage="encoding"
            yield event(stage,complete=False)
            import numpy as np
            queries=np.asarray((models or Models(False)).text_vecs(semantic_texts),dtype=np.float32)
            if queries.shape!=(len(semantic_texts),384) or not np.isfinite(queries).all(): raise ValueError("invalid_query_vectors")
            queries=queries/np.maximum(np.linalg.norm(queries,axis=1,keepdims=True),1e-12)
            check()
            ann=Path(os.environ.get("SEARCHFU_ANN_DIR",str(Path(db).parent/"ann")))
            semantic=[{} for _ in queries]
            if early:
                from candidates import discover
                directory=Path(os.environ.get('SEARCHFU_CANDIDATES_DIR',str(Path(db).parent/'candidates')))
                stage='candidates'
                try:
                    discovered,progress=discover(c,directory,queries,pool,where,params,check,ef=candidate_ef)
                except SearchCancelled: raise
                except Exception:
                    check();discovered=None;progress={'reason':'candidate_cache_unusable'}
                if discovered is not None:
                    semantic=discovered;last_results=results(lexical,semantic)
                    yield event(stage,last_results,complete=False,coverage='approximate_candidates',**progress)
                    if rerank_early and (rerank_model or reranker):
                        from reranking import rerank,LocalReranker
                        stage='reranked_early';reranker=reranker or LocalReranker(rerank_model)
                        try:
                            last_results,n=rerank(c,lexical,semantic,semantic_texts[0],top_k,rerank_limit,reranker,check,mix=rerank_mix,where=where,params=params,collections=selected)
                            yield event(stage,last_results,complete=False,coverage='candidates_only',passages_scored=n)
                        except SearchCancelled: raise
                        except Exception:
                            check();yield event(stage,complete=False,reason='reranker_unavailable')
                else: yield event(stage,complete=False,coverage='skipped',**progress)
                check()
            last_emit=0.0
            stage="semantic"
            matrix_used=False
            scoped_vectors=False
            if (ann/"ann.meta.json").is_file():
                with _matrix_lock(ann,check):
                    # Full rebuild may have invalidated metadata while we waited.
                    if (ann/"ann.meta.json").is_file():
                        scoped_vectors=_prefer_scoped_vectors(c,ann,where,params)
                    if (ann/"ann.meta.json").is_file() and not scoped_vectors:
                        matrix_used=True
                        for semantic,last_progress in _matrix_pass(c,ann,queries,pool,where,params,check,batch_rows,initial=semantic):
                            if time.monotonic()-last_emit>=emit_seconds:
                                check();last_results=results(lexical,semantic)
                                yield event(stage,last_results,complete=False,coverage="partial",**last_progress)
                                last_emit=time.monotonic()
            if not matrix_used:
                stage="deep"
                yield event(stage,complete=False,reason="scope_smaller_than_matrix" if scoped_vectors else "matrix_missing_using_indexed_vectors")
                for semantic,last_progress in _full_pass(c,queries,pool,where,params,check,batch_rows,initial=semantic):
                    if time.monotonic()-last_emit>=emit_seconds:
                        check();last_results=results(lexical,semantic)
                        yield event(stage,last_results,complete=False,coverage="partial",**last_progress)
                        last_emit=time.monotonic()
            check();last_results=results(lexical,semantic)
            coverage="quantized_matrix_and_sqlite_tail" if matrix_used else "full_precision_indexed_vectors"
            yield event(stage,last_results,complete=False,stage_complete=True,coverage=coverage,**last_progress)
            if matrix_used:
                stage="refined"
                semantic=_refine(c,queries,semantic,lexical,pool,check)
                check();last_results=results(lexical,semantic)
                yield event(stage,last_results,complete=False,stage_complete=True,coverage="full_precision_candidates_only")
            if deep and matrix_used:
                stage="deep";last_emit=0
                for semantic,last_progress in _full_pass(c,queries,pool,where,params,check,batch_rows,initial=semantic):
                    if time.monotonic()-last_emit>=emit_seconds:
                        check();last_results=results(lexical,semantic)
                        yield event(stage,last_results,complete=False,coverage="partial",**last_progress)
                        last_emit=time.monotonic()
                check();last_results=results(lexical,semantic)
                coverage="full_precision_indexed_vectors"
                yield event(stage,last_results,complete=False,stage_complete=True,coverage=coverage,**last_progress)
            if rerank_model or reranker:
                from reranking import rerank,LocalReranker
                stage='reranked';reranker=reranker or LocalReranker(rerank_model)
                try:
                    last_results,n=rerank(c,lexical,semantic,semantic_texts[0],top_k,rerank_limit,reranker,check,mix=rerank_mix,where=where,params=params,collections=selected)
                    yield event(stage,last_results,complete=False,coverage='candidates_only',passages_scored=n)
                except SearchCancelled: raise
                except Exception:
                    check();yield event(stage,complete=False,reason='reranker_unavailable')
            yield event("done",last_results,complete=True,coverage=coverage,index_health=snapshot,ranking_coverage="reranked_candidates_only" if last_results and "relevance_score" in last_results[0] else "retrieved_candidates")
    except SearchCancelled as exc:
        yield event("cancelled",last_results,complete=False,reason=str(exc),interrupted_stage=stage,coverage="partial")
    except Exception:
        if (cancelled and cancelled()) or (deadline is not None and time.monotonic()>=deadline):
            yield event("cancelled",last_results,complete=False,reason="cancelled" if cancelled and cancelled() else "budget_exhausted",interrupted_stage=stage,coverage="partial")
        else: raise


def vector_candidates(db,qv,k,where,params):
    """Legacy verifier adapter. Caller already owns the shared matrix lock."""
    import numpy as np
    from search import connect
    ann=Path(os.environ.get("SEARCHFU_ANN_DIR",str(Path(db).parent/"ann")))
    if not (ann/"ann.meta.json").is_file(): return None
    with closing(connect(Path(db),readonly=True)) as c:
        c.execute("BEGIN")
        best=[{}]
        for best,_ in _matrix_pass(c,ann,np.asarray([qv],dtype=np.float32),k,where,params,lambda:None,8192): pass
        out=[]
        for row in _ranked_rows(best[0])[:k]:
            cid,p,kind,mt,mime,score=row
            text=c.execute("SELECT text FROM chunks WHERE id=?",(cid,)).fetchone()[0]
            out.append((cid,text,p,kind,mt,mime,score))
        return out
