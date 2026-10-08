"""Shared bounded refresh and exclusive bulk-indexing GPU admission."""
from contextlib import contextmanager
import json,os,threading,urllib.request

REFRESH_BATCH = 8
REFRESH_ALLOCATOR_MIB = 256


def call(route,body,timeout=90):
    req=urllib.request.Request(os.environ.get('SEARCHFU_ARCHON','http://127.0.0.1:8790')+route,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=timeout) as response:return json.load(response)
    except Exception:return {'ok':False}


@contextmanager
def shared_lease():
    """Bounded refresh keeps the brain resident; never exclusive fallback."""
    response=call('/lease/shared/acquire',{'pid':os.getpid()},timeout=2)
    token=response.get('token')
    if not response.get('ok') or not token:raise RuntimeError('gpu_lease_unavailable')
    try:
        if (response.get('allocator_mib') != REFRESH_ALLOCATOR_MIB or
                response.get('batch_size') != REFRESH_BATCH):
            raise RuntimeError('gpu_lease_unavailable')
        def check():
            if not call('/lease/shared/check',{'token':token},timeout=2).get('ok'):
                raise RuntimeError('gpu_lease_lost')
        yield check
    finally:
        # Ownership remains pinned until process exit frees its CUDA context.
        if not call('/lease/shared/release',{'token':token},timeout=2).get('ok'):
            raise RuntimeError('gpu_lease_release_unconfirmed')


@contextmanager
def lease():
    response=call('/lease/acquire',{'ttl_secs':3600,'priority':True})
    token=response.get('token')
    if not response.get('ok') or not token:raise RuntimeError('gpu_lease_unavailable')
    stop=threading.Event();lost=threading.Event()
    def renew():
        while not stop.wait(300):
            if not call('/lease/renew',{'token':token,'ttl_secs':3600}).get('ok'):
                lost.set();return
    worker=threading.Thread(target=renew,daemon=True);started=False
    def check():
        if lost.is_set():raise RuntimeError('gpu_lease_lost')
    try:
        worker.start();started=True
        yield check
    finally:
        stop.set()
        if started:worker.join(timeout=95)
        if not call('/lease/release',{'token':token}).get('ok'):raise RuntimeError('gpu_lease_release_unconfirmed')
