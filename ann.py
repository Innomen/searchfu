#!/usr/bin/env python3
"""Build compact vector caches from canonical SQLite embeddings.

Queries scan the cache exhaustively; quantization can change rankings. Original
float32 vectors remain in SQLite. Incremental builds append new chunk IDs;
--full recompacts the cache from SQLite without reading any source files.
Build locks exclude readers. Metadata is invalidated before mutations, so a
failed rebuild cannot advertise a partially replaced cache as complete.
"""
from __future__ import annotations
import argparse, json, os, re, sys, time, traceback
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import corpus_filter

VAULT = Path(os.environ.get("SEARCHFU_ROOT", "."))
DATA = Path(os.environ.get("SEARCHFU_DIR", str(Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "searchfu")))
# SEARCHFU_ANN_DB / SEARCHFU_ANN_DIR let a build read from a fast DB copy and
# write the matrix to a fast dir, without touching the live index.
DB = Path(os.environ["SEARCHFU_ANN_DB"]) if os.environ.get("SEARCHFU_ANN_DB") else Path(os.environ.get("SEARCHFU_DB", str(DATA / "drive.sqlite3")))
ANN = Path(os.environ["SEARCHFU_ANN_DIR"]) if os.environ.get("SEARCHFU_ANN_DIR") else DATA / "ann"
BLOCKS = ANN / "blocks"
PROGRESS = ANN / "ann.progress.json"
META = ANN / "ann.meta.json"
SCALE_FILE = ANN / "ann.scale.json"
BLOCK_ROWS = int(os.environ.get("SEARCHFU_ANN_BLOCK", "1000000"))
DIM = 384
BLOB_LEN = DIM * 4
SCALE_MARGIN = 1.10          # headroom so incremental appends rarely clip
FORMAT_VERSION = 2
DTYPE_BITS = {"fp16": 16, "int8": 8, "int4": 4}


def ts():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _evict(path):
    """Drop a fully consumed file from page cache."""
    try:
        with open(path, "rb") as f:
            os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
    except OSError:
        pass


DB_ROW_BYTES = 1600 * 1.2


def _evict_db_prefix(rows):
    """Drop page cache for the DB prefix already scanned by `rows` rows."""
    try:
        with open(str(DB), "rb") as f:
            os.posix_fadvise(f.fileno(), 0, int(rows * DB_ROW_BYTES),
                             os.POSIX_FADV_DONTNEED)
    except OSError:
        pass


class Log:
    def __init__(self, census_only):
        self.f = open(str(DATA / ("ann-census.log" if census_only else "ann-build.log")), "a")

    def beat(self, d):
        try:
            self.f.write(json.dumps(d) + "\n")
            self.f.flush()
        except OSError as e:
            print(f"[ann] log write failed: {e}", file=sys.stderr)

    def crash(self, exc):
        try:
            self.f.write(json.dumps({"crash": True, "ts": ts(),
                                     "error": "matrix_build_failed", "exception_class": type(exc).__name__}) + "\n")
            self.f.flush()
        except OSError:
            pass

    def close(self):
        self.f.close()


def load_manifest(c):
    """file_id -> normalized path, one scan of the files table."""
    m = {}
    for fid, path in c.execute("SELECT id, path FROM files"):
        m[fid] = corpus_filter.normalize(path)
    return m


def iter_chunks(c, start=0, batch=20000):
    """Yield (rowid, file_id, embedding blob) in rowid order from `start`."""
    while True:
        rows = c.execute(
            "SELECT rowid, file_id, embedding FROM chunks WHERE rowid >= ? ORDER BY rowid LIMIT ?",
            (start, batch)).fetchall()
        if not rows:
            return
        yield from rows
        start = rows[-1][0] + 1
        if len(rows) < batch:
            return


def _norm_rows(e):
    """L2-normalize a (N, DIM) fp32 matrix row-wise (matches stored vectors)."""
    norms = np.linalg.norm(e, axis=1)
    safe = np.where(norms == 0, 1.0, norms)[:, None]
    return e / safe


def quantize(e, scale, dtype):
    """Quantize an (N, DIM) L2-normalized fp32 matrix."""
    if dtype == "fp16":
        return e.astype(np.float16)
    if dtype == "int8":
        q = np.round(e / scale[None, :] * 127.0).clip(-127, 127)
        return q.astype(np.int8)
    if dtype == "int4":
        q = np.round(e / scale[None, :] * 7.0).clip(-7, 7).astype(np.int8)
        hi = (q[:, 0::2] + 7) & 0xF
        lo = (q[:, 1::2] + 7) & 0xF
        return ((hi << 4) | lo).astype(np.uint8)
    raise ValueError(f"unknown dtype {dtype}")


def compute_scale(c, manifest, log):
    """Full pass: per-dim max |value| over all non-junk, L2-normalized chunks."""
    max_abs = np.zeros(DIM, dtype=np.float64)
    scanned = 0
    start = 0
    t0 = time.time()
    while True:
        rows = c.execute(
            "SELECT rowid, file_id, embedding FROM chunks WHERE rowid >= ? ORDER BY rowid LIMIT ?",
            (start, 20000)).fetchall()
        if not rows:
            break
        embs = []
        for cid, fid, blob in rows:
            if blob is None or len(blob) != BLOB_LEN:
                continue
            if corpus_filter.is_junk(manifest.get(fid, "")):
                continue
            embs.append(np.frombuffer(blob, dtype="<f4"))
        if embs:
            e = _norm_rows(np.stack(embs).astype(np.float32))
            max_abs = np.maximum(max_abs, np.max(np.abs(e), axis=0))
            scanned += e.shape[0]
        start = rows[-1][0] + 1
        if scanned and scanned % 2000000 < 20000:
            dt = time.time() - t0
            log.beat({"scale_pass": True, "ts": ts(), "rows": scanned,
                      "mbps": round(scanned * BLOB_LEN / 1e6 / dt, 2) if dt > 0 else 0})
    scale = (max_abs * SCALE_MARGIN).astype(np.float32)
    scale = np.where(scale == 0, 1.0, scale)
    return scale, scanned


def load_progress():
    if PROGRESS.is_file() and any(BLOCKS.glob("emb-*.npy")):
        try:
            return json.loads(PROGRESS.read_text())
        except (OSError, ValueError):
            return None
    return None


def load_meta():
    if META.is_file():
        try:
            return json.loads(META.read_text())
        except (OSError, ValueError):
            return None
    return None


def save_scale(scale, dtype):
    tmp = SCALE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"dtype": dtype,
                               "scale": [float(s) for s in scale], "ts": ts()}))
    tmp.replace(SCALE_FILE)


def load_scale(dtype):
    if SCALE_FILE.is_file():
        try:
            d = json.loads(SCALE_FILE.read_text())
            if d.get("dtype") == dtype and d.get("scale"):
                return np.asarray(d["scale"], dtype=np.float32)
        except (OSError, ValueError):
            pass
    return None


from storage import require_capacity

def run_build(census_only, full=False, dtype="int8"):
    import fcntl
    if not census_only:
        require_capacity(ANN, phase="matrix_start")
    ANN.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    with open(ANN / ".build.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH if census_only else fcntl.LOCK_EX)
        return _run_build(census_only, full, dtype)

def _run_build(census_only, full=False, dtype="int8"):
    import sqlite3
    t0 = time.time()
    if not census_only:
        BLOCKS.mkdir(parents=True, exist_ok=True)
    log = Log(census_only)
    log.beat({"start": True, "ts": ts(), "pid": os.getpid(),
              "census_only": census_only, "full": full, "dtype": dtype})
    try:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        c.execute("BEGIN")
        manifest = load_manifest(c)
        log.beat({"manifest_files": len(manifest), "ts": ts()})
        identity = c.execute("SELECT value FROM index_state WHERE key='index_id'").fetchone()
        identity = str(identity[0]) if identity else None
        approx_total = c.execute("SELECT MAX(rowid) FROM chunks").fetchone()[0] or 0

        meta = None if full else load_meta()
        prog = None if (full or census_only) else load_progress()
        start = (prog["flushed_upto"] + 1) if prog else 0

        if not full and not census_only:
            if prog and not meta and not prog.get("dtype"):
                raise ValueError("legacy_resume_requires_full_build")
            for previous in (meta, prog):
                if previous and previous.get("dtype", (meta or {}).get("dtype", "fp16")) != dtype:
                    raise ValueError("dtype_change_requires_full_build")
                if previous and previous.get("index_id") and previous["index_id"] != identity:
                    raise ValueError("matrix_index_mismatch")
                if previous and previous.get("filter_version",1) != corpus_filter.FILTER_VERSION:
                    raise ValueError("filter_change_requires_full_build")
            if meta and not prog and int(meta.get("total_kept",0)):
                raise ValueError("missing_resume_state_requires_full_build")
        # Readers holding the shared lock finish first. Future readers fall back
        # to canonical SQLite if this generation never reaches publication.
        if not census_only:
            META.unlink(missing_ok=True)

        if full and not census_only:
            for p in list(BLOCKS.glob("emb-*.npy")) + list(BLOCKS.glob("ids-*.npy")):
                p.unlink()
            for p in (BLOCKS / "ids.npy", PROGRESS, SCALE_FILE):
                if p.is_file():
                    p.unlink()
            log.beat({"full_wipe": True, "ts": ts()})

        scale = None
        if not full:
            if meta and meta.get("dtype") == dtype and meta.get("scale"):
                scale = np.asarray(meta["scale"], dtype=np.float32)
                log.beat({"scale_reused": True, "ts": ts(), "source": "meta"})
            else:
                s = load_scale(dtype)
                if s is not None:
                    scale = s
                    log.beat({"scale_reused": True, "ts": ts(), "source": "scale_file"})
        if scale is None:
            if not census_only:
                log.beat({"scale_pass": "start", "ts": ts()})
            scale, scale_rows = compute_scale(c, manifest, log)
            if not census_only:
                save_scale(scale, dtype)
            log.beat({"scale_pass": "done", "ts": ts(), "rows": scale_rows,
                      "max_scale": round(float(scale.max()), 4),
                      "min_scale": round(float(scale.min()), 4)})

        kept = prog["kept"] if prog else 0
        pruned = prog["pruned"] if prog else 0
        skipped = prog["skipped"] if prog else 0
        bi = prog["bi"] if prog else 0
        flushed_upto = prog["flushed_upto"] if prog else 0
        if prog:
            log.beat({"resumed": True, "ts": ts(), "from_rowid": start,
                      "blocks_so_far": bi, "kept_so_far": kept})

        buf = []
        id_rows = []
        if prog:
            id_rows = [np.load(str(BLOCKS / ("ids-%04d.npy" % i))) for i in range(bi)]

        def flush_block():
            nonlocal buf, bi, flushed_upto
            if not buf:
                return
            ids = np.array([r[0] for r in buf], dtype=np.int64)
            e = _norm_rows(np.stack([r[1] for r in buf]).astype(np.float32))
            if not census_only:
                require_capacity(BLOCKS, len(buf) * (DIM * 4 + 16), phase="matrix_block")
                np.save(str(BLOCKS / ("emb-%04d.npy" % bi)), quantize(e, scale, dtype))
                np.save(str(BLOCKS / ("ids-%04d.npy" % bi)), ids)
                id_rows.append(ids)
                _evict(str(BLOCKS / ("emb-%04d.npy" % bi)))
                _evict(str(BLOCKS / ("ids-%04d.npy" % bi)))
                _evict_db_prefix(flushed_upto)
            flushed_upto = int(ids[-1])
            bi += 1
            buf = []
            if not census_only:
                PROGRESS.write_text(json.dumps({"flushed_upto": flushed_upto,
                                                "kept": kept, "pruned": pruned,
                                                "dtype": dtype, "index_id": identity,
                                                "filter_version": corpus_filter.FILTER_VERSION,
                                                "skipped": skipped, "bi": bi,
                                                "ts": ts()}))

        for cid, fid, blob in iter_chunks(c, start=start):
            if blob is None or len(blob) != BLOB_LEN:
                skipped += 1
                log.beat({"bad_blob": True, "ts": ts(), "rowid": cid, "len": len(blob)})
            elif corpus_filter.is_junk(manifest.get(fid, "")):
                pruned += 1
            else:
                buf.append((cid, np.frombuffer(blob, dtype="<f4")))
                kept += 1
                if len(buf) >= BLOCK_ROWS:
                    flush_block()
            total = kept + pruned + skipped
            if total % 250000 == 0:
                dt = time.time() - t0
                mbps = (total * BLOB_LEN) / 1e6 / dt if dt > 0 else 0.0
                eta_h = ((approx_total - total) * BLOB_LEN / 1e6 / mbps / 3600) if mbps > 0 else None
                log.beat({"ts": ts(), "rows": total, "kept": kept, "pruned": pruned,
                          "skipped": skipped, "mbps": round(mbps, 2),
                          "eta_h": round(eta_h, 2) if eta_h is not None else None})
        flush_block()
        if census_only:
            log.beat({"done": True, "ts": ts(), "kept": kept, "pruned": pruned,
                      "skipped": skipped, "elapsed_s": round(time.time() - t0, 1)})
            return
        ids = np.concatenate(id_rows) if id_rows else np.zeros(0, dtype=np.int64)
        require_capacity(BLOCKS, ids.nbytes + 65536, phase="matrix_publication")
        with open(BLOCKS / "ids.npy.tmp", "wb") as f:
            np.save(f, ids)
        os.replace(BLOCKS / "ids.npy.tmp", BLOCKS / "ids.npy")
        meta = {
            "format_version": FORMAT_VERSION, "index_id": identity,
            "total_kept": int(kept), "pruned": int(pruned), "skipped": int(skipped),
            "rows_per_block": BLOCK_ROWS, "n_blocks": bi, "dim": DIM,
            "dtype": dtype, "scale": [float(s) for s in scale],
            "scale_margin": SCALE_MARGIN,
            "db_max_rowid": approx_total,
            "filter_version": corpus_filter.FILTER_VERSION, "build_ts": ts(),
        }
        temp_meta = META.with_suffix(".json.tmp")
        temp_meta.write_text(json.dumps(meta, indent=1))
        os.replace(temp_meta, META)
        log.beat({"done": True, "ts": ts(), "total_kept": kept, "pruned": pruned,
                  "skipped": skipped, "dtype": dtype,
                  "elapsed_s": round(time.time() - t0, 1)})
    except Exception as e:
        log.crash(e)
        raise
    finally:
        if "c" in locals(): c.close()
        log.close()


def run_verify(n=20, no_filter=False):
    """Measure quantized chunk recall against stored float32 vectors.

    Uses sampled stored embeddings as queries, not human relevance labels. This
    checks compression accuracy only, without loading models or reading sources.
    """
    import sqlite3
    import fcntl
    from search import dequant_block
    from retrieval import _metadata
    if n<1: raise ValueError("invalid_sample_count")
    with open(ANN / ".build.lock", "a") as lock:
        fcntl.flock(lock,fcntl.LOCK_SH)
        with sqlite3.connect(DB.resolve().as_uri()+"?mode=ro",uri=True) as c:
            c.execute("BEGIN")
            meta=_metadata(ANN,c)
            manifest=load_manifest(c)
            samples=c.execute("SELECT embedding FROM chunks WHERE length(embedding)=? ORDER BY random() LIMIT ?",(BLOB_LEN,n)).fetchall()
            if not samples: raise ValueError("empty_vector_index")
            q=_norm_rows(np.stack([np.frombuffer(row[0],dtype='<f4') for row in samples]))
            exact=[{} for _ in samples]; approx=[{} for _ in samples]
            def retain(best, ids, scores):
                for qi in range(len(samples)):
                    merged=list(best[qi].items())+[(int(cid),float(score)) for cid,score in zip(ids,scores[:,qi])]
                    best[qi]=dict(sorted(merged,key=lambda item:(-item[1],item[0]))[:30])
            for rows in _verify_rows(c):
                valid=[r for r in rows if r[2] is not None and len(r[2])==BLOB_LEN and (no_filter or not corpus_filter.is_junk(manifest.get(r[1],"")))]
                if valid:
                    e=_norm_rows(np.stack([np.frombuffer(r[2],dtype='<f4') for r in valid]))
                    retain(exact,[r[0] for r in valid],e@q.T)
            ids=np.load(BLOCKS/"ids.npy",mmap_mode='r')
            offset=0
            for bi in range(meta['n_blocks']):
                block=np.load(BLOCKS/("emb-%04d.npy"%bi),mmap_mode='r')
                for lo in range(0,len(block),8192):
                    part=block[lo:lo+8192];bid=ids[offset+lo:offset+lo+len(part)]
                    ph=','.join('?'*len(bid))
                    live={int(cid) for cid,fid in c.execute("SELECT id,file_id FROM chunks WHERE id IN ("+ph+")",[int(x) for x in bid]) if no_filter or not corpus_filter.is_junk(manifest.get(fid,""))}
                    e=_norm_rows(dequant_block(part,meta));mask=np.array([int(x) in live for x in bid])
                    retain(approx,bid[mask],(e@q.T)[mask])
                offset+=len(block)
            # Tail vectors are scored at original precision, as in public search.
            for rows in _verify_rows(c,int(meta.get('db_max_rowid',0))+1):
                valid=[r for r in rows if r[2] is not None and len(r[2])==BLOB_LEN and (no_filter or not corpus_filter.is_junk(manifest.get(r[1],"")))]
                if valid:
                    e=_norm_rows(np.stack([np.frombuffer(r[2],dtype='<f4') for r in valid]))
                    retain(approx,[r[0] for r in valid],e@q.T)
            recalls=[len(set(a)&set(b))/len(a) for a,b in zip(exact,approx) if a]
            mean=sum(recalls)/len(recalls) if recalls else 0
            print(json.dumps({"recall_at_30_mean":round(mean,4),"n_queries":len(samples),
                              "reference":"stored_float32_vectors","dtype":meta.get('dtype','fp16'),
                              "filter":not no_filter,"pass":mean>=.9}))

def _verify_rows(c,start=0):
    cursor=c.execute("SELECT id,file_id,embedding FROM chunks WHERE id>=? ORDER BY id",(start,))
    while True:
        rows=cursor.fetchmany(8192)
        if not rows: return
        yield rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--full", action="store_true",
                   help="rebuild cache from SQLite embeddings; never reads source files")
    b.add_argument("--dtype", choices=["int8", "fp16", "int4"], default="int8")
    sub.add_parser("census")
    v = sub.add_parser("verify")
    v.add_argument("--n", type=int, default=20)
    v.add_argument("--no-filter", action="store_true",
                   help="include indexed junk in the full-precision reference")
    a = ap.parse_args()
    if a.cmd == "build":
        run_build(False, full=a.full, dtype=a.dtype)
    elif a.cmd == "census":
        run_build(True)
    elif a.cmd == "verify":
        run_verify(a.n, no_filter=a.no_filter)


if __name__ == "__main__":
    try: main()
    except Exception as exc:
        print(json.dumps({"ok":False,"error":"matrix_operation_failed","exception_class":type(exc).__name__}),file=sys.stderr)
        sys.exit(1)
