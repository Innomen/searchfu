"""Private, local background search lifecycle. No service or model server needed."""
from __future__ import annotations
from pathlib import Path
import argparse
import json
import os
import re
import signal
import threading
import sys
import time
import uuid

TERMINAL = {"done", "cancelled", "failed"}


def add_stage_options(p):
    p.add_argument("--scope",action="append",default=[],dest="scopes",help="named indexed collection; repeat to search the union")
    p.add_argument("--names",action="store_true",dest="names_only",help="literal stored filename/path search; no models")
    p.add_argument("--collections-file",help=argparse.SUPPRESS)
    p.add_argument("--expand",action="append",default=[],dest="expansions",help="another short query for the same topic; up to 7")
    p.add_argument("--lex",action="append",dest="lexical_queries",help="override keyword wording; repeat for variants")
    p.add_argument("--semantic",action="append",dest="semantic_queries",help="override semantic wording; repeat for variants")
    p.add_argument("--early",action="store_true",help="try optional HNSW candidates before the complete scan")
    p.add_argument("--candidate-ef",type=int,default=500)
    p.add_argument("--rerank-model",help="offline local CPU cross-encoder model name/path")
    p.add_argument("--rerank-limit",type=int,default=36)
    p.add_argument("--rerank-early",action="store_true",help="also rerank the early graph pool; adds another inference pass")
    p.add_argument("--rerank-mix",type=float,default=1.0,help="0 keeps retrieval order; 1 uses learned order")
    p.add_argument("--deltas",action="store_true",help="send evidence changes; terminal events retain snapshots")
    p.add_argument("--deep",action="store_true",help="also scan every eligible stored full-precision vector")
    p.add_argument("--max-seconds",type=float,help="total search budget; preserve already delivered results")
    p.add_argument("--batch-rows",type=int,default=8192,help=argparse.SUPPRESS)
    p.add_argument("--emit-seconds",type=float,default=1.0,help=argparse.SUPPRESS)


def add_commands(sub):
    for name in ("stream", "start"):
        p=sub.add_parser(name,help="progressive indexed search" if name=="stream" else "start a background indexed search")
        p.add_argument("query")
        p.add_argument("--top-k",type=int,default=12)
        p.add_argument("--kind",choices=["all","text"],default="all")
        p.add_argument("--after");p.add_argument("--before");p.add_argument("--path")
        p.add_argument("--fts",action="store_true",dest="fts_only")
        p.add_argument("--and",action="store_true",dest="require_all")
        add_stage_options(p)
    p=sub.add_parser("poll",help="read new search events")
    p.add_argument("job_id");p.add_argument("--after",type=int,default=0,dest="after_sequence")
    p.add_argument("--limit",type=int,default=32)
    p=sub.add_parser("cancel",help="stop search and retain delivered events")
    p.add_argument("job_id")


def _root():
    base=Path(os.environ.get("SEARCHFU_JOBS_DIR",str(Path(os.environ.get("XDG_STATE_HOME",str(Path.home()/".local/state")))/"searchfu/jobs")))
    source=Path(__file__).resolve().parent
    if base.resolve().is_relative_to(source): raise ValueError("job_content_must_live_outside_source")
    return base


def _job(job_id):
    if not re.fullmatch(r"[0-9a-f]{32}",job_id): raise ValueError("invalid_job_id")
    path=_root()/job_id
    if not path.is_dir() or path.is_symlink(): raise ValueError("unknown_job")
    return path


def _write(path,value):
    temp=path.with_suffix(path.suffix+".tmp")
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,"w") as f: json.dump(value,f);f.write("\n")
    temp.replace(path)


def _read(path):
    return json.loads(path.read_text())


def _identity(pid):
    # Linux process start tick prevents signalling an unrelated reused PID.
    try:
        text=Path(f"/proc/{int(pid)}/stat").read_text()
        fields=text.rsplit(")",1)[1].split()
        if fields[0]=="Z": return None
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()+":"+fields[19]
    except (OSError,ValueError,IndexError): return None


def _alive(state):
    ident=state.get("process_identity")
    return bool(ident and _identity(state.get("pid",0))==ident)


def _options(args):
    names=("top_k","kind","after","before","path","fts_only","require_all",
           "expansions","deep","max_seconds","batch_rows","emit_seconds",
           "scopes","collections_file","names_only","lexical_queries","semantic_queries","early","candidate_ef","rerank_model","rerank_limit","rerank_mix","rerank_early","deltas")
    defaults={'scopes':[],'collections_file':None,'names_only':False,'lexical_queries':None,'semantic_queries':None,'early':False,'candidate_ef':500,'rerank_model':None,'rerank_limit':36,'rerank_mix':1.0,'rerank_early':False,'deltas':False}
    return {name:getattr(args,name,defaults.get(name)) for name in names}


def _validate(options):
    if options["top_k"]<1 or options["batch_rows"]<1 or options["emit_seconds"]<0:
        raise ValueError("invalid_search_limits")
    if options["max_seconds"] is not None and options["max_seconds"]<=0:
        raise ValueError("invalid_search_budget")
    if len(options["expansions"])>7: raise ValueError("too_many_query_variants")
    for name in ('lexical_queries','semantic_queries'):
        values=options.get(name)
        if values is not None and (not values or len(set(values))>8): raise ValueError('invalid_query_variants')
    if not 0<=options.get('rerank_mix',1)<=1: raise ValueError('invalid_rerank_mix')
    if options.get('candidate_ef',500)<1: raise ValueError('invalid_candidate_ef')
    limit=options.get('rerank_limit',36)
    if limit<1 or limit>200 or (options.get('rerank_model') and limit<options['top_k']):
        raise ValueError('invalid_rerank_limit')


def start(db,args):
    options=_options(args);_validate(options)
    if not Path(db).is_file(): raise ValueError("index_missing")
    root=_root();root.mkdir(parents=True,exist_ok=True,mode=0o700)
    job_id=uuid.uuid4().hex
    directory=root/job_id;directory.mkdir(mode=0o700)
    _write(directory/"request.json",{"db":str(Path(db).absolute()),"query":args.query,"options":options,
                                    "ann":os.environ.get("SEARCHFU_ANN_DIR")})
    _write(directory/"state.json",{"status":"starting","created":time.time()})
    # Query text is in an owner-only request, never worker argv or logs.
    command=[sys.executable,str(Path(__file__).resolve()),"--worker",str(directory)]
    actions=[(os.POSIX_SPAWN_OPEN,0,os.devnull,os.O_RDONLY,0),
             (os.POSIX_SPAWN_OPEN,1,os.devnull,os.O_WRONLY,0),
             (os.POSIX_SPAWN_OPEN,2,os.devnull,os.O_WRONLY,0)]
    pid=os.posix_spawn(sys.executable,command,dict(os.environ),file_actions=actions,setsid=True)
    def reap():
        try: os.waitpid(pid,0)
        except ChildProcessError: pass
    threading.Thread(target=reap,daemon=True).start()
    # The worker owns state transitions; parent must not overwrite an early done.
    return {"job_id":job_id,"status":"started","source_tree_walked":False}


def _events(directory,after,limit):
    out=[]
    path=directory/"events.ndjson"
    if not path.exists(): return out
    with path.open() as f:
        for line in f:
            # A concurrent append may expose an unfinished line; retry next poll.
            if not line.endswith("\n"): break
            event=json.loads(line)
            if event["sequence"]>after:
                out.append(event)
                if len(out)>=limit: break
    return out


def poll(job_id,after=0,limit=32):
    if after<0 or not 1<=limit<=100: raise ValueError("invalid_poll_limits")
    directory=_job(job_id);state=_read(directory/"state.json")
    events=_events(directory,after,limit)
    status=state["status"]
    if status not in TERMINAL and state.get("process_identity") and not _alive(state):
        # Terminal event can precede atomic state publication.
        last=None
        path=directory/"events.ndjson"
        if path.exists():
            with path.open() as f:
                for line in f:
                    if line.endswith("\n"): last=json.loads(line)
        status=last["stage"] if last and last["stage"] in TERMINAL else "failed"
    # Startup death before worker registration is bounded, too.
    if status=="starting" and time.time()-state["created"]>10: status="failed"
    return {"job_id":job_id,"status":status,"events":events,
            "next_sequence":events[-1]["sequence"] if events else after}


def cancel(job_id):
    directory=_job(job_id);state=_read(directory/"state.json")
    if state["status"] in TERMINAL: return {"job_id":job_id,"status":state["status"]}
    # Cancellation is durable even if it arrives before worker registration.
    fd=os.open(directory/"cancel",os.O_CREAT|os.O_WRONLY,0o600);os.close(fd)
    if _alive(state):
        try: os.kill(state["pid"],signal.SIGTERM)
        except ProcessLookupError: pass
    return {"job_id":job_id,"status":"cancellation_requested"}


def worker(directory):
    from retrieval import retrieve,SearchCancelled
    os.umask(0o077)
    request=_read(directory/"request.json")
    if request.get("ann"): os.environ["SEARCHFU_ANN_DIR"]=request["ann"]
    stopping=False
    def stop(signum,frame):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    def cancelled(): return stopping or (directory/"cancel").exists()
    state={"status":"running","pid":os.getpid(),"process_identity":_identity(os.getpid()),"created":time.time()}
    _write(directory/"state.json",state)
    sequence=0;terminal="failed"
    with (directory/"events.ndjson").open("a") as output:
        try:
            for event in retrieve(Path(request["db"]),request["query"],cancelled=cancelled,**request["options"]):
                sequence+=1;event["sequence"]=sequence
                output.write(json.dumps(event)+"\n");output.flush()
                if event["stage"] in TERMINAL: terminal=event["stage"]
        except Exception as exc:
            sequence+=1
            output.write(json.dumps({"sequence":sequence,"stage":"failed","complete":False,
                                     "error":"search_failed","exception_class":type(exc).__name__,
                                     "source_tree_walked":False})+"\n");output.flush()
        finally:
            _write(directory/"state.json",{**state,"status":terminal,"finished":time.time(),"events":sequence})
            # Preserve results for polling, discard the original query request.
            (directory/"request.json").unlink(missing_ok=True)


def dispatch(args,db):
    if args.cmd=="stream":
        from retrieval import retrieve
        options=_options(args);_validate(options)
        stopping=False
        def stop(signum,frame):
            nonlocal stopping
            stopping=True
        signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
        for seq,event in enumerate(retrieve(db,args.query,cancelled=lambda:stopping,**options),1):
            print(json.dumps({"sequence":seq,**event}),flush=True)
    elif args.cmd=="start": print(json.dumps(start(db,args)))
    elif args.cmd=="poll": print(json.dumps(poll(args.job_id,args.after_sequence,args.limit)))
    else: print(json.dumps(cancel(args.job_id)))

if __name__=="__main__":
    if len(sys.argv)!=3 or sys.argv[1]!="--worker": sys.exit(2)
    worker(Path(sys.argv[2]))
