#!/usr/bin/env python3
"""Local hybrid text retrieval.

CLI and shell defaults use SEARCHFU_DIR or the XDG user data directory.
The searchfu.sh wrapper uses the configured DB and ANN locations.
Runtime is offline by default; --allow-model-download is required for
a missing model. The wrapper runs text-only and CPU-only by default.
"""
from __future__ import annotations

import argparse, hashlib, json, mimetypes, os, re, sqlite3, stat, struct, sys, time
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
os.environ.setdefault("PYTORCH_ALLOC_CONF", "expandable_segments:True")
from dataclasses import dataclass
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import corpus_filter
from extraction import DOCUMENT_EXTS
from storage import require_capacity, batch_bytes

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

# Default vault root and index directory. Override via env vars.
VAULT = Path(os.environ.get("SEARCHFU_ROOT", "."))
DATA = Path(os.environ.get("SEARCHFU_DIR", str(Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "searchfu")))
TEXT_EXTS = {".md", ".txt", ".rst", ".org", ".py", ".js", ".ts", ".json", ".html", ".css", ".sh", ".toml", ".yaml", ".yml", ".csv", ".srt", ".vtt"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
# Directories to skip during walks. Extend as needed for your corpus.
SKIP_NAMES = {".git", ".cache", ".vault_rag", ".smart-env", ".obsidian", "node_modules", "__pycache__",
              ".venv", "site-packages", ".npm", ".cargo", ".rustup",
              ".pyenv", ".Trash-1000"}

def profile_db(profile: str) -> Path:
    return Path(os.environ.get("SEARCHFU_DB", str(DATA / "drive.sqlite3")))

def connect(db: Path, readonly=False) -> sqlite3.Connection:
    if readonly:
        c = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
        c.execute("PRAGMA query_only=ON")
        return c
    db.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript("""
      CREATE TABLE IF NOT EXISTS files(
        id INTEGER PRIMARY KEY, path TEXT UNIQUE, root TEXT, kind TEXT, mime TEXT,
        size INTEGER, mtime REAL, content_sig TEXT, indexed_at REAL,
        text_embedding BLOB, image_embedding BLOB);
      CREATE TABLE IF NOT EXISTS chunks(
        id INTEGER PRIMARY KEY, file_id INTEGER REFERENCES files(id) ON DELETE CASCADE,
        ordinal INTEGER, text TEXT, embedding BLOB);
      CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, content='chunks', content_rowid='id');
      CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN INSERT INTO chunks_fts(rowid,text) VALUES(new.id,new.text); END;
      CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN INSERT INTO chunks_fts(chunks_fts,rowid,text) VALUES('delete',old.id,old.text); END;
      CREATE TABLE IF NOT EXISTS build_progress(
        file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
        source_sig TEXT NOT NULL, next_ordinal INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS index_state(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
      INSERT OR IGNORE INTO index_state VALUES('chunk_high_water', 0);
      UPDATE index_state SET value=MAX(value, COALESCE((SELECT MAX(id) FROM chunks),0)) WHERE key='chunk_high_water';
      CREATE TRIGGER IF NOT EXISTS chunks_water AFTER INSERT ON chunks BEGIN
        UPDATE index_state SET value=MAX(value,new.id) WHERE key='chunk_high_water';
      END;
      CREATE INDEX IF NOT EXISTS files_mtime ON files(mtime);
      CREATE INDEX IF NOT EXISTS files_kind ON files(kind);
      CREATE INDEX IF NOT EXISTS chunks_file ON chunks(file_id);
    """)
    # Bind new matrix generations to this canonical index; legacy data is migrated
    # only during an explicit indexing operation, never during search.
    c.execute("INSERT OR IGNORE INTO index_state VALUES('index_id', lower(hex(randomblob(16))))")
    if 'extractor' not in {r[1] for r in c.execute('PRAGMA table_info(files)')}:
        c.execute("ALTER TABLE files ADD COLUMN extractor TEXT NOT NULL DEFAULT 'plain-text-v1'")
    columns={r[1] for r in c.execute("PRAGMA table_info(files)")}
    for name,kind in (("content_hash","TEXT"),("source_device","INTEGER")):
        if name not in columns: c.execute(f"ALTER TABLE files ADD COLUMN {name} {kind}")
    c.execute("CREATE INDEX IF NOT EXISTS files_content_hash ON files(content_hash)")
    c.commit()
    return c

def pack(v) -> bytes:
    return struct.pack(f"<{len(v)}f", *[float(x) for x in v])

def unpack(b: bytes | None):
    if not b: return None
    import numpy as np
    return np.frombuffer(b, dtype="<f4")

def chunks(text: str, size=1200, overlap=180):
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text: return []
    out=[]; pos=0
    while pos < len(text):
        end=min(len(text), pos+size)
        if end < len(text):
            cut=max(text.rfind("\n\n", pos, end), text.rfind(". ", pos, end))
            if cut > pos+size//2: end=cut+1
        out.append(text[pos:end].strip())
        if end >= len(text): break
        pos=max(pos+1, end-overlap)
    return [x for x in out if len(x) >= 40]

class Models:
    def __init__(self, allow_download=False):
        self.allow=allow_download; self._text=None; self._clip=None; self._proc=None
    def _online(self):
        if self.allow:
            os.environ.pop("HF_HUB_OFFLINE", None); os.environ.pop("TRANSFORMERS_OFFLINE", None)
    def text(self):
        if self._text is None:
            self._online()
            from sentence_transformers import SentenceTransformer
            self._text=SentenceTransformer("all-MiniLM-L6-v2", device="cuda" if os.environ.get("SEARCHFU_CUDA")=="1" else "cpu", local_files_only=not self.allow)
        return self._text
    def clip(self):
        if self._clip is None:
            self._online()
            from transformers import CLIPModel, CLIPProcessor
            name="openai/clip-vit-base-patch32"
            self._clip=CLIPModel.from_pretrained(name, local_files_only=not self.allow)
            self._proc=CLIPProcessor.from_pretrained(name, local_files_only=not self.allow)
            self._clip.eval()
        return self._clip, self._proc
    def text_vecs(self, items):
        model = self.text()
        bs = 128 if getattr(self, "cpu_fallback", False) else 8
        if getattr(self, "cpu_fallback", False):
            return model.encode(items, batch_size=bs, normalize_embeddings=True, show_progress_bar=False)
        try:
            return model.encode(items, batch_size=bs, normalize_embeddings=True, show_progress_bar=False)
        except Exception as e:
            msg = str(e).lower()
            if os.environ.get("SEARCHFU_CUDA")=="1" or not ("cuda" in msg and "out of memory" in msg): raise
            import torch
            torch.cuda.empty_cache()
            try:
                return model.encode(items, batch_size=bs, normalize_embeddings=True, show_progress_bar=False)
            except Exception as e2:
                msg2 = str(e2).lower()
                if os.environ.get("SEARCHFU_CUDA")=="1" or not ("cuda" in msg2 and "out of memory" in msg2): raise
                print(json.dumps({"warn": "CUDA OOM twice: encoding on CPU for the rest of this run"}), flush=True)
                self.cpu_fallback = True
                model.to("cpu")
                return model.encode(items, batch_size=bs, normalize_embeddings=True, show_progress_bar=False)
    def image_vec(self, path):
        import torch
        from PIL import Image
        m,p=self.clip()
        with Image.open(path) as im, torch.inference_mode():
            inp=p(images=im.convert("RGB"), return_tensors="pt")
            v=m.get_image_features(**inp)[0].float(); v=v/v.norm()
        return v.cpu().numpy()
    def clip_text(self, query):
        import torch
        m,p=self.clip()
        with torch.inference_mode():
            inp=p(text=[query], return_tensors="pt", padding=True)
            v=m.get_text_features(**inp)[0].float(); v=v/v.norm()
        return v.cpu().numpy()

def iter_files(roots, walk_errors=None, excluded=(), devices=None, all_files=False):
    excluded=[str(Path(p).absolute()).rstrip("/") for p in excluded]
    def denied(p): return any(str(p)==x or str(p).startswith(x+"/") for x in excluded)
    for root in (Path(r).absolute() for r in roots):
        if not root.is_dir() or denied(root):
            if walk_errors is not None: walk_errors.append(True)
            continue
        for base, dirs, names in os.walk(root, followlinks=False, onerror=lambda e: walk_errors.append(True) if walk_errors is not None else None):
            dirs[:] = [d for d in dirs if d not in SKIP_NAMES and not denied(Path(base)/d) and not (Path(base)/d).is_symlink()]
            if devices and str(root) in devices:
                retained=[]
                for d in dirs:
                    try:
                        if (Path(base)/d).stat().st_dev in (devices[str(root)] if isinstance(devices[str(root)],list) else [devices[str(root)]]): retained.append(d)
                    except OSError:
                        if walk_errors is not None: walk_errors.append(True)
                dirs[:]=retained
            for name in names:
                p=Path(base)/name
                if denied(p) or p.is_symlink(): continue
                ext=p.suffix.lower()
                if all_files or ext in TEXT_EXTS or ext in IMAGE_EXTS or ext in DOCUMENT_EXTS:
                    yield root,p,"image" if ext in IMAGE_EXTS else "text" if ext in TEXT_EXTS or ext in DOCUMENT_EXTS else "file"

def file_sig(st): return f"{st.st_size}:{st.st_mtime_ns}"

def _file_items(root, p, kind, st, max_sub):
    """Read + chunk one text file into stream items. Runs in a worker thread."""
    if p.suffix.lower() in DOCUMENT_EXTS:
        from extraction import extract
        raw,extractor=extract(p)
    else:
        payload=p.read_bytes();raw=payload.decode("utf-8",errors="ignore");extractor='plain-text-v1'
    digest=hashlib.sha256(payload if p.suffix.lower() not in DOCUMENT_EXTS else p.read_bytes()).hexdigest()
    if file_sig(p.stat())!=file_sig(st): raise ValueError("source_changed_during_read")
    cl = chunks(raw)
    meta = {"root": str(root), "path": str(p), "kind": kind, "st": st, "extractor":extractor,"content_hash":digest}
    if len(cl) <= max_sub:
        return [dict(meta, chunks=cl, base=0, final=True)]
    items = []
    for i in range(0, len(cl), max_sub):
        items.append(dict(meta, chunks=cl[i:i + max_sub], base=i, final=(i + max_sub >= len(cl))))
    return items

def build(db, roots, allow_download=False, images=True, max_files=0, **policy):
    # A writer owns both resumable file state and monotonic chunk IDs.
    # Advisory lock does not block readers and is released on process death.
    import fcntl
    db = Path(db); db.parent.mkdir(parents=True, exist_ok=True)
    with open(str(db) + ".build.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _build(db, roots, allow_download, images, max_files, **policy)

def _build(db, roots, allow_download=False, images=True, max_files=0, *, excluded=(), devices=None, names_only=False):
    """Pipeline: main thread walks + signature-checks; worker threads read and
    chunk files concurrently; the main thread batches chunks across files into
    encode calls and writes them back. Reads overlap with embedding.
    Large files are read and chunked in memory, then encoded in sub-batches."""
    import concurrent.futures as cf
    require_capacity(Path(db).parent, phase="database_start")
    roots = [str(Path(r).absolute()) for r in roots]
    c = connect(db); models = Models(allow_download)
    # Old matrices may remember IDs above the current DB maximum after a
    # deletion. Reserve their watermark before allocating any new IDs.
    ann = Path(os.environ.get("SEARCHFU_ANN_DIR", str(Path(db).parent / "ann")))
    high = 0
    for name, key in (("ann.meta.json", "db_max_rowid"), ("ann.progress.json", "flushed_upto")):
        try: high = max(high, int(json.loads((ann/name).read_text()).get(key, 0)))
        except (OSError, ValueError, TypeError): pass
    with c:
        c.execute("UPDATE index_state SET value=MAX(value,?) WHERE key='chunk_high_water'", (high,))
        c.execute("INSERT OR REPLACE INTO index_state VALUES('last_build_started',?)", (time.time(),))
        # One-time repair of provably truncated legacy multi-batch files.
        if not c.execute("SELECT 1 FROM index_state WHERE key='stream_repair_v1'").fetchone():
            c.execute("UPDATE files SET content_sig=NULL,indexed_at=NULL WHERE id IN (SELECT file_id FROM chunks GROUP BY file_id HAVING MIN(ordinal)>0)")
            c.execute("INSERT INTO index_state VALUES('stream_repair_v1',1)")
    def _ts(): return time.strftime("%Y-%m-%dT%H:%M:%S%z")
    print(json.dumps({"run_start": True, "ts": _ts(), "pid": os.getpid(), "root_count": len(roots)}), flush=True)
    workers = int(os.environ.get("SEARCHFU_WORKERS", "6"))
    batch_target = int(os.environ.get("SEARCHFU_BATCH", "1536"))
    max_sub = int(os.environ.get("SEARCHFU_MAX_SUB", "25000"))
    c.execute('CREATE TEMP TABLE seen_paths(path TEXT PRIMARY KEY) WITHOUT ROWID')
    changed = 0; skipped = 0; errors = 0; counted = set()
    ex = cf.ThreadPoolExecutor(max_workers=workers)
    jobs = {}; queue_items = []; queue_chunks = 0; last_flush = time.time()
    gpu_context=None;gpu_check=lambda:None

    def flush(items):
        nonlocal changed, queue_items, queue_chunks, last_flush, gpu_context, gpu_check
        if not items: return
        for it in items:
            if len(it["chunks"]) >= 2000:
                print(json.dumps({"embedding": True, "chunks": len(it["chunks"]), "base": it.get("base", 0)}), flush=True)
        for it in items:
            path = it["path"]; sig = file_sig(it["st"])
            row = c.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
            checkpoint = c.execute("SELECT source_sig,next_ordinal FROM build_progress WHERE file_id=?", (row[0],)).fetchone() if row else None
            resume = checkpoint[1] if checkpoint and checkpoint[0] == sig else 0
            original_base = it.get("base", 0)
            skip = max(0, resume - original_base)
            if skip >= len(it["chunks"]) and it["chunks"]:
                continue
            it = dict(it, chunks=it["chunks"][skip:], base=original_base + skip)
            require_capacity(Path(db).parent, batch_bytes(it["chunks"]), phase="before_embedding")
            reused=None
            if it.get("base",0)==0 and it["final"] and it.get("content_hash"):
                twin=c.execute("SELECT id FROM files WHERE content_hash=? AND extractor=? AND path<>? AND indexed_at IS NOT NULL LIMIT 1",(it["content_hash"],it.get("extractor","plain-text-v1"),path)).fetchone()
                if twin:
                    bank=list(c.execute("SELECT text,embedding FROM chunks WHERE file_id=? ORDER BY ordinal",(twin[0],)))
                    if [t for t,b in bank]==it["chunks"] and all(b and len(b)==1536 for t,b in bank):reused=[unpack(b) for t,b in bank]
            if reused is None and it["chunks"] and os.environ.get("SEARCHFU_CUDA")=="1" and gpu_context is None:
                from gpu_lease import lease
                gpu_context=lease();gpu_check=gpu_context.__enter__()
            gpu_check()
            vs = reused if reused is not None else models.text_vecs(it["chunks"]) if it["chunks"] else []
            gpu_check()
            require_capacity(Path(db).parent, batch_bytes(it["chunks"]), phase="before_commit")
            mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
            row = c.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
            fid = row[0] if row else None
            with c:
                if fid is None:
                    cur = c.execute("INSERT INTO files(path,root,kind,mime,size,mtime,content_sig,indexed_at,text_embedding,image_embedding) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                    (path, it["root"], it["kind"], mime, it["st"].st_size, it["st"].st_mtime, None, None, None, None))
                    fid = cur.lastrowid
                elif it.get("base", 0) == 0:
                    c.execute("DELETE FROM chunks WHERE file_id=?", (fid,))
                    c.execute("UPDATE files SET content_sig=NULL,indexed_at=NULL WHERE id=?", (fid,))
                high = c.execute("SELECT value FROM index_state WHERE key='chunk_high_water'").fetchone()[0]
                for i, (t, v) in enumerate(zip(it["chunks"], vs)):
                    c.execute("INSERT INTO chunks(id,file_id,ordinal,text,embedding) VALUES(?,?,?,?,?)", (high+i+1, fid, it.get("base", 0) + i, t, pack(v)))
                c.execute("INSERT OR REPLACE INTO build_progress VALUES(?,?,?)",
                          (fid, sig, it.get("base", 0) + len(it["chunks"])))
                if it["final"]:
                    c.execute("DELETE FROM build_progress WHERE file_id=?", (fid,))
                    c.execute("UPDATE files SET root=?,kind=?,mime=?,size=?,mtime=?,content_sig=?,indexed_at=? WHERE id=?",
                              (it["root"], it["kind"], it["mime"] if hasattr(it, "mime") else mimetypes.guess_type(it["path"])[0] or "application/octet-stream", it["st"].st_size, it["st"].st_mtime, sig, time.time(), fid))
                c.execute("UPDATE files SET extractor=?,source_device=?,content_hash=? WHERE id=?",(it.get("extractor","plain-text-v1"),it["st"].st_dev,it.get("content_hash") if it["final"] else None,fid))
            if it["final"] and path not in counted:
                counted.add(path); changed += 1
        queue_items = []; queue_chunks = 0; last_flush = time.time()

    err_log = open(str(Path(db).with_suffix(".errors.log")), "a")
    def drain(fut):
        nonlocal errors, queue_items, queue_chunks
        its = None
        try: its = fut.result()
        except Exception:
            errors += 1
            if errors <= 25:
                err_log.write(json.dumps({"phase": "read", "error": "file_read_failed"}) + "\n"); err_log.flush()
        if its:
            queue_items.extend(its); queue_chunks += sum(len(x["chunks"]) for x in its)

    walk_errors = []
    try:
        for n, (root, p, kind) in enumerate(iter_files(roots, walk_errors,excluded,devices,all_files=True), 1):
            if max_files and n > max_files: break
            try: st = p.stat()
            except OSError:
                walk_errors.append(True); continue
            if not stat.S_ISREG(st.st_mode): continue
            path = str(p)
            c.execute('INSERT OR IGNORE INTO seen_paths VALUES(?)',(path,))
            c.commit()
            if corpus_filter.is_junk(path):
                skipped += 1
                continue
            old = c.execute("SELECT id,content_sig,content_hash FROM files WHERE path=?", (path,)).fetchone()
            if old and old[1] == file_sig(st) and (kind!="text" or old[2]):
                skipped += 1
            elif names_only or kind=="file" or (kind=="image" and not images) or st.st_size>int(os.environ.get("SEARCHFU_MAX_TEXT_BYTES",str(64*1024**2))):
                with c:
                    c.execute("INSERT INTO files(path,root,kind,mime,size,mtime,content_sig,indexed_at,source_device) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET root=excluded.root,kind=excluded.kind,mime=excluded.mime,size=excluded.size,mtime=excluded.mtime,source_device=excluded.source_device",(path,str(root),kind,mimetypes.guess_type(path)[0],st.st_size,st.st_mtime,None,None,st.st_dev))
                    fid=c.execute("SELECT id FROM files WHERE path=?",(path,)).fetchone()[0]
                    if old and old[1] is not None and old[1]!=file_sig(st):
                        c.execute("DELETE FROM chunks WHERE file_id=?",(fid,));c.execute("UPDATE files SET content_hash=NULL,content_sig=NULL,indexed_at=NULL WHERE id=?",(fid,))
                changed+=1
            elif kind == "text":
                if len(jobs) >= workers * 2:
                    done, _ = cf.wait(list(jobs), return_when=cf.FIRST_COMPLETED)
                    for f in done: drain(f); del jobs[f]
                jobs[ex.submit(_file_items, root, p, kind, st, max_sub)] = True
            elif images and kind == "image":
                require_capacity(Path(db).parent, 1024*1024, phase="image_write")
                try:
                    image_emb = pack(models.image_vec(p))
                    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
                    with c:
                        if old:
                            fid = old[0]
                            c.execute("UPDATE files SET root=?,kind=?,mime=?,size=?,mtime=?,content_sig=?,indexed_at=?,text_embedding=?,image_embedding=? WHERE id=?",
                                      (str(root), kind, mime, st.st_size, st.st_mtime, file_sig(st), time.time(), None, image_emb, fid))
                        else:
                            c.execute("INSERT INTO files(path,root,kind,mime,size,mtime,content_sig,indexed_at,text_embedding,image_embedding) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                      (path, str(root), kind, mime, st.st_size, st.st_mtime, file_sig(st), time.time(), None, image_emb))
                    changed += 1
                except Exception:
                    errors += 1
            else:
                continue
            for f in [f for f in list(jobs) if f.done()]: drain(f); del jobs[f]
            now = time.time()
            if queue_chunks >= batch_target or (queue_items and now - last_flush > 3.0):
                flush(queue_items)
            if n == 1 or n % 25 == 0:
                print(json.dumps({"scanned": n, "ts": _ts(), "changed": changed, "unchanged": skipped, "errors": errors, "queued_chunks": queue_chunks}), flush=True)

        for f in cf.wait(list(jobs)).done: drain(f)
        flush(queue_items)
        ex.shutdown(wait=True)
        # Prune only roots included in this completed pass.
        if not max_files and not walk_errors:
            for root in map(str, roots):
                if not Path(root).is_dir(): continue
                with c:
                    c.execute('DELETE FROM files WHERE root=? AND NOT EXISTS (SELECT 1 FROM seen_paths WHERE seen_paths.path=files.path)',(root,))
        with c:
            for key, value in {"last_build_finished": time.time(), "last_build_errors": errors,
                               "last_build_changed": changed, "last_build_unchanged": skipped,
                               "last_build_walk_errors": len(walk_errors)}.items():
                c.execute("INSERT OR REPLACE INTO index_state VALUES(?,?)", (key,value))
        print(json.dumps({"done": True, "ts": _ts(), "changed": changed, "unchanged": skipped, "errors": errors, "walk_errors": len(walk_errors)}), flush=True)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": "database_build_failed",
                          "exception_class": type(exc).__name__,
                          "sqlite_errorcode": getattr(exc, "sqlite_errorcode", None),
                          "sqlite_errorname": getattr(exc, "sqlite_errorname", None)}), flush=True)
        raise
    finally:
        ex.shutdown(wait=True, cancel_futures=True)
        err_log.close()
        c.close()
        if gpu_context is not None: gpu_context.__exit__(None,None,None)

def _date(s, end=False):
    if not s:return None
    from datetime import datetime
    d=datetime.fromisoformat(s)
    return d.timestamp() + (86399 if end and len(s)==10 else 0)

def dequant_block(emb, meta):
    """Dequantize a loaded matrix block to an (N, DIM) fp32 array. meta
    carries the dtype and per-dim scale (format v2); v1 metas (no dtype)
    are plain fp16."""
    import numpy as np
    dtype = meta.get("dtype", "fp16")
    if dtype == "fp16":
        return emb.astype(np.float32)
    scale = np.asarray(meta.get("scale", [1.0] * 384), dtype=np.float32)
    if dtype == "int8":
        return emb.astype(np.float32) / 127.0 * scale[None, :]
    if dtype == "int4":
        p = emb
        hi = (p >> 4).astype(np.float32) - 7.0
        lo = (p & 0xF).astype(np.float32) - 7.0
        out = np.empty((p.shape[0], 384), dtype=np.float32)
        out[:, 0::2] = hi
        out[:, 1::2] = lo
        return out / 7.0 * scale[None, :]
    return emb.astype(np.float32)


def _ann_candidates(db, qv, k, where, params, target_files=None):
    import fcntl
    ann = Path(os.environ.get("SEARCHFU_ANN_DIR", str(Path(db).parent / "ann")))
    if not (ann / "ann.meta.json").exists(): return None
    with open(ann / ".build.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        return _ann_candidates_read(db, qv, k, where, params, target_files)

def _ann_candidates_read(db, qv, k, where, params, target_files=None):
    """Compatibility adapter for the matrix verification command.

    Uses the same indexed vector engine as the public search commands. Its
    caller holds the matrix lock; no source file is opened here.
    """
    from retrieval import vector_candidates
    return vector_candidates(db, qv, k, where, params)


def stats(db):
    """Corpus census via one fast scan of the files table."""
    import collections
    c = connect(db, readonly=True)
    paths = [p for (p,) in c.execute("SELECT path FROM files")]
    trees = collections.Counter(); ext = collections.Counter(); junk = 0
    for p in paths:
        np_ = corpus_filter.normalize(p)
        trees[np_.split("/", 1)[0] if "/" in np_ else "(root)"] += 1
        base = np_.rsplit("/", 1)[-1]
        ext[base.rsplit(".", 1)[-1].lower() if "." in base else "(none)"] += 1
        if corpus_filter.is_junk(p): junk += 1
    return {"db": str(db), "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "chunks": c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
            "files": len(paths), "files_by_tree": dict(trees.most_common()),
            "extensions_sample": dict(ext.most_common(16)),
            "junk_files": junk, "junk_filter_version": corpus_filter.FILTER_VERSION}

def search(db, query, top_k=12, kind="all", after=None, before=None, path=None,
           images=True, models=None, fts_only=False, require_all=False, **retrieval_options):
    """Blocking convenience wrapper around the progressive text engine.

    Image search remains an explicit legacy Python-only feature; the public
    wrapper and progressive interface are text-only.
    """
    from retrieval import retrieve
    result = []
    for event in retrieve(db, query, top_k=top_k, kind=kind, after=after,
                          before=before, path=path, fts_only=fts_only,
                          require_all=require_all, models=models, **retrieval_options):
        if "results" in event: result = event["results"]
    if images and kind in ("all", "image") and not fts_only:
        models = models or Models(False)
        from contextlib import closing
        import numpy as np
        with closing(connect(db, readonly=True)) as c:
            iq = models.clip_text(query)
            from retrieval import filters
            where, params = filters(kind="image", after=after, before=before, path=path)
            from collections_config import scope_sql
            sw,sp,_=scope_sql(retrieval_options.get('scopes',()),retrieval_options.get('collections_file'))
            where+=sw;params+=sp
            sql = "SELECT path,kind,mtime,mime,image_embedding FROM files f WHERE " + " AND ".join(where)
            for p,k,mt,mime,emb in c.execute(sql,params):
                if emb and not corpus_filter.is_junk(p):
                    result.append({"score":float(np.dot(iq,unpack(emb))),"snippet":None,
                                   "path":p,"kind":k,"mtime":mt,"mime":mime,"match":"vision"})
        result = sorted(result,key=lambda r:r["score"],reverse=True)[:top_k]
    return result


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--profile",choices=["vault","system"],default="vault"); ap.add_argument("--db",type=Path)
    sub=ap.add_subparsers(dest="cmd",required=True)
    b=sub.add_parser("build"); b.add_argument("roots",nargs="*"); b.add_argument("--allow-model-download",action="store_true"); b.add_argument("--no-images",action="store_true"); b.add_argument("--max-files",type=int,default=0)
    q=sub.add_parser("search"); q.add_argument("query"); q.add_argument("--top-k",type=int,default=12); q.add_argument("--kind",choices=["all","text","image"],default="all"); q.add_argument("--after"); q.add_argument("--before"); q.add_argument("--path"); q.add_argument("--no-images",action="store_true"); q.add_argument("--fts",action="store_true",help="FTS5 only: no model load, near-instant"); q.add_argument("--and",action="store_true",dest="require_all",help="FTS: require every query term in a chunk")
    from jobs import add_commands, add_stage_options
    add_stage_options(q)
    add_commands(sub)
    s=sub.add_parser("status"); s.add_argument("--scope",action="append",default=[]); s.add_argument("--agent",action="store_true",help="payload-free index health; no paths or result text")
    sub.add_parser("stats")
    a=ap.parse_args(); db=a.db or profile_db(a.profile)
    if a.cmd=="build":
        roots=a.roots or ([str(VAULT)] if a.profile=="vault" else [str(Path.home())])
        build(db,roots,a.allow_model_download,not a.no_images,a.max_files)
    elif a.cmd=="search": print(json.dumps(search(db,a.query,a.top_k,a.kind,a.after,a.before,a.path,not a.no_images,fts_only=a.fts,require_all=a.require_all, **{key:getattr(a,key) for key in ("scopes","collections_file","names_only","expansions","lexical_queries","semantic_queries","early","candidate_ef","rerank_model","rerank_limit","rerank_mix","rerank_early","deltas","deep","max_seconds","batch_rows","emit_seconds")}),indent=2))
    elif a.cmd in {"stream", "start", "poll", "cancel"}:
        from jobs import dispatch
        dispatch(a, db)
    elif a.cmd=="stats": print(json.dumps(stats(db),indent=2))
    else:
        c=connect(db, readonly=True)
        try: state=dict(c.execute("SELECT key,value FROM index_state WHERE key LIKE 'last_build_%'"))
        except sqlite3.OperationalError: state={}
        status={"files":c.execute("SELECT count(*) FROM files").fetchone()[0],
                "chunk_id_upper_bound":c.execute("SELECT MAX(rowid) FROM chunks").fetchone()[0] or 0,
                "chunks":c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
                "bytes":db.stat().st_size if db.exists() else 0, "update":state}
        status["update_incomplete"]=state.get("last_build_started",0)>state.get("last_build_finished",0)
        if a.scope:
            from collections_config import scope_sql
            where,params,_=scope_sql(a.scope)
            condition=' AND '.join(where)
            status['files']=c.execute('SELECT count(*) FROM files f WHERE '+condition,params).fetchone()[0]
            status['chunks']=c.execute('SELECT count(*) FROM chunks c JOIN files f ON f.id=c.file_id WHERE '+condition,params).fetchone()[0]
            status['content_files']=c.execute('SELECT count(DISTINCT f.id) FROM files f JOIN chunks c ON c.file_id=f.id WHERE '+condition,params).fetchone()[0]
        if not a.agent:
            status["db"]=str(db)
        print(json.dumps(status)); c.close()
if __name__=="__main__":
    try: main()
    except (KeyboardInterrupt, BrokenPipeError):
        sys.exit(130)
    except Exception as exc:
        print(json.dumps({"ok":False,"error":"search_operation_failed","exception_class":type(exc).__name__}),file=sys.stderr)
        sys.exit(1)
