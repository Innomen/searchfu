# searchfu Database Format Specification

Version 1.1 — 2026-10-06

This document specifies the on-disk format of the searchfu index. Tools that
read or write this format must conform to these invariants.

## Directory layout

```
$SEARCHFU_DIR/
  drive.sqlite3           SQLite database (canonical)
  drive.sqlite3-wal       WAL journal (auto-managed)
  drive.sqlite3-shm       Shared memory (auto-managed)
  drive.sqlite3.build.lock  Advisory lock file (build writer)
  drive.sqlite3.update.lock  Advisory lock file (update writer)
  drive.sqlite3.errors.log  Build error log (append-only)
  ann/
    blocks/
      emb-0000.npy        Quantized embedding block 0
      emb-0001.npy        Quantized embedding block 1
      ...
      ids-0000.npy        Chunk IDs for block 0
      ids-0001.npy        Chunk IDs for block 1
      ...
      ids.npy             Concatenated chunk IDs (all blocks)
    ann.progress.json     Build progress (resume state)
    ann.meta.json         Matrix metadata (totals, scale, dtype)
    ann.scale.json        Cached per-dim scale (survives rebuilds)
    .build.lock           Advisory lock file (matrix writer)
  ann-build.log           Build heartbeat log (append-only)
  ann-census.log          Census heartbeat log (append-only)
```

## SQLite schema

### files

```sql
CREATE TABLE files(
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE,          -- absolute or root-relative path
  root TEXT,                 -- walk root that produced this file
  kind TEXT,                 -- "text" or "image"
  mime TEXT,                 -- guessed MIME type
  size INTEGER,              -- file size in bytes
  mtime REAL,                -- modification time (Unix epoch)
  content_sig TEXT,          -- "size:mtime_ns" signature for change detection
  indexed_at REAL,           -- last index time (Unix epoch)
  text_embedding BLOB,       -- float32 vector (384 dims, L2-normalized) or NULL
  image_embedding BLOB       -- float32 vector (CLIP) or NULL
);
CREATE INDEX files_mtime ON files(mtime);
CREATE INDEX files_kind ON files(kind);
```

### chunks

```sql
CREATE TABLE chunks(
  id INTEGER PRIMARY KEY,    -- monotonic, never reused (matrix key)
  file_id INTEGER REFERENCES files(id) ON DELETE CASCADE,
  ordinal INTEGER,           -- position within the file (0-based)
  text TEXT,                 -- chunk text
  embedding BLOB             -- float32 vector (384 dims, L2-normalized)
);
CREATE INDEX chunks_file ON chunks(file_id);
```

**Critical invariant**: chunk IDs are allocated from a high-water mark
(`index_state.chunk_high_water`) and are never reused, even after file
deletion. The vector matrix is keyed by chunk ID; reusing IDs would corrupt
the matrix.

### chunks_fts (FTS5 virtual table)

```sql
CREATE VIRTUAL TABLE chunks_fts USING fts5(
  text,
  content='chunks',
  content_rowid='id'
);
```

Maintained by triggers:

```sql
CREATE TRIGGER chunks_ai AFTER INSERT ON chunks
  BEGIN INSERT INTO chunks_fts(rowid,text) VALUES(new.id,new.text); END;
CREATE TRIGGER chunks_ad AFTER DELETE ON chunks
  BEGIN INSERT INTO chunks_fts(chunks_fts,rowid,text)
    VALUES('delete',old.id,old.text); END;
```

### build_progress

```sql
CREATE TABLE build_progress(
  file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
  source_sig TEXT NOT NULL,
  next_ordinal INTEGER NOT NULL
);
```

Resumable build state. Cleared when a file's build completes.

### index_state

```sql
CREATE TABLE index_state(
  key TEXT PRIMARY KEY,
  value INTEGER NOT NULL
);
```

Keys:
- `chunk_high_water` — highest chunk ID allocated (monotonic; allocate above it)
- `index_id` — random canonical index identity (TEXT value in the non-STRICT table)
- `last_build_started` — Unix epoch of last build start
- `last_build_finished` — Unix epoch of last build finish
- `last_build_errors` — error count from last build
- `last_build_changed` — changed file count from last build
- `last_build_unchanged` — skipped file count from last build
- `last_build_walk_errors` — walk error count from last build
- `stream_repair_v1` — one-time migration flag (1 = done)

## Embedding format

- **Model**: `all-MiniLM-L6-v2` (384 dimensions)
- **Storage**: little-endian float32, 384 values per vector (1536 bytes)
- **Normalization**: L2-normalized (unit length)
- **Pack/unpack**: `struct.pack("<384f", ...)` / `np.frombuffer(b, dtype="<f4")`

## Vector matrix format

### Block layout

- `blocks/emb-NNNN.npy`: quantized embeddings, one block per
  `SEARCHFU_ANN_BLOCK` rows (default 1,000,000)
- `blocks/ids-NNNN.npy`: int64 chunk IDs, same order as embeddings
- `blocks/ids.npy`: concatenated int64 chunk IDs (all blocks, written at
  build completion)

### Quantization

Per-dimension symmetric quantization:

```
scale[d] = max|x[:,d]| * 1.10   (over the initial full build)
```

| Dtype  | Encoding                                    | Bytes/elem |
|--------|---------------------------------------------|------------|
| fp16   | `x.astype(np.float16)`                      | 2          |
| int8   | `round(x / scale[d] * 127).clip(-127,127)`  | 1          |
| int4   | `round(x / scale[d] * 7).clip(-7,7)`, packed 2/byte | 0.5 |

Dequantization:

```
fp16:  x = emb.astype(float32)
int8:  x = emb.astype(float32) / 127.0 * scale[d]
int4:  x = unpack(emb) / 7.0 * scale[d]   (two 4-bit values per byte)
```

### Metadata

`ann.meta.json`:

```json
{
  "format_version": 2,
  "index_id": "canonical-db-identity",
  "total_kept": 25000000,
  "pruned": 123456,
  "skipped": 789,
  "rows_per_block": 1000000,
  "n_blocks": 25,
  "dim": 384,
  "dtype": "int8",
  "scale": [0.123, 0.456, ...],   // 384 floats
  "scale_margin": 1.10,
  "db_max_rowid": 25000000,
  "filter_version": 1,
  "build_ts": "2026-10-06T12:00:00+0000"
}
```

`ann.progress.json` (resume and incremental append state, retained after successful build):

```json
{
  "flushed_upto": 24000000,
  "dtype": "int8",
  "index_id": "canonical-db-identity",
  "filter_version": 1,
  "kept": 24000000,
  "pruned": 120000,
  "skipped": 700,
  "bi": 24,
  "ts": "2026-10-06T11:59:00+0000"
}
```

### Freshness contract

The matrix covers chunk IDs from 1 to `db_max_rowid`. Query-time:

1. Score the matrix (covers IDs ≤ `db_max_rowid`).
2. Score the DB tail (IDs > `db_max_rowid`) by reading embeddings from
   SQLite directly.
3. Merge results.

A stale matrix (DB has grown past `db_max_rowid`) degrades gracefully: the
tail scan covers the gap. A missing matrix falls back to a full DB scan.

## Junk filter

`corpus_filter.py` defines the filter. It is applied at three layers:

1. **Build time**: junk files are not chunked or embedded (new files only;
   existing junk rows are kept).
2. **Matrix build time**: junk chunks are excluded from the matrix.
3. **Query time**: junk candidates are dropped from results.

The filter is versioned (`FILTER_VERSION`). The matrix meta records the
filter version at build time. A version mismatch fails search and requires rebuilding the cache from SQLite.
Changing dtype also requires a full rebuild or a separate cache directory.

## Backwards compatibility

- **Format version 1** (no `dtype` in meta): plain fp16 matrix. Still
  readable by v2 code.
- **Format version 2** (current): adds `dtype`, `scale`, `scale_margin`.
- **Chunk ID monotonicity**: any tool that writes to the DB must respect
  the high-water mark. Never reuse a chunk ID.
- **FTS5 content table**: the FTS table is an external-content FTS5 table that
  reads from `chunks`. Do not add a separate FTS build step; the triggers
  handle it.

## What is NOT in the format

- No HNSW graph or ANN index. The matrix is the index.
- No separate "files" vector table. File-level embeddings are in
  `files.text_embedding` and `files.image_embedding`.
- No multi-tenancy. A DB may contain several explicitly indexed roots.
- No encryption. The DB is plaintext SQLite.

## Search and publication contracts

Search input is exclusively this index: no source traversal, source stat or
source reads. SQLite read snapshots hold a consistent canonical view. Cache
locks exclude mutation; metadata is removed before cache mutations and
published only on success. Failure therefore leaves a missing-cache fallback
rather than an apparently complete but partially replaced generation. Full
cache rebuilds read SQLite embeddings only. Original vectors are retained to
support new cache/backends without a drive crawl or source re-embedding.

New caches record `index_id` and reject a different DB identity. Legacy caches
without this binding remain readable; a full cache rebuild adds it. Interrupted
legacy resume state without sufficient dtype metadata requires a full rebuild.

All precision levels are lossy compared with original float32 vectors. Search
normalizes decoded vectors before cosine scoring. Keyword/semantic file lists
use reciprocal-rank fusion, with constant 60. Candidate refinement scores
original vectors but does not establish exhaustive original-vector recall.
The optional deep pass does. Coverage is always limited to indexed material,
query variants, scope and filter rules, never a proof of topic completeness.

Background job state is outside the source tree, in owner-only directories and
files. NDJSON events have monotonically increasing sequence numbers. Polling
returns events after a cursor and a next cursor. A concurrent partial line is
not returned until completed. Terminal states are done/cancelled/failed;
previous events survive. Queries are removed on normal worker termination;
results persist until the user removes the job directory. Neither is encrypted.

## Progressive extensions (2026-10-06)

Writable schema migration adds `files.extractor TEXT NOT NULL DEFAULT
'plain-text-v1'`; current values also include `docx-text-v1` and `pdf-text-v1`.
Readonly retrieval remains compatible with older schemas. Extraction occurs only
at explicit indexing. DOCX uses bounded UTF-8 ZIP/XML parsing with DTD/entity
rejection; PDF uses optional pypdf, bounded size/pages and no automatic OCR.

Optional candidates live outside source in a distinct graph directory, selected
by `SEARCHFU_CANDIDATES_DIR` or the database parent's `candidates` directory.
Immutable `graph-UUID.bin` generations are published by atomic
`candidates.meta.json` replacement under a cooperative build/read lock. Metadata
binds index identity, filter version, dimensions and representation. Failed or
capacity-refused builds preserve the prior pointer; incompatible caches are
skipped during search. Construction consumes stored original vectors only and
is capped by default. Updated/new rows are still covered by the subsequent index
pass. Graph seed scores use original cosine values; later compressed discovery
scores are approximate until finalist refinement or the full float32 pass.

`result_id` and optional event changes follow docs/ai-protocol.md. Snapshot
identity is file-based; chunk/snippet/rank changes produce updates. Independent
lexical and semantic variants, graph discovery, rerank blend and optional early
reranking are shared by blocking, streaming and background search. Keyword-only
search terminates before optional semantic/model stages.

## Collection/catalog extensions

Writable migration adds nullable `files.content_hash TEXT` (SHA-256 of original
supported source bytes) and `files.source_device INTEGER`. Legacy readonly
unscoped retrieval remains supported; scoped device filtering requires the
migrated catalog. External version-1 collection configuration stores roots,
priorities, searchability, exclusions and optional allowed device identities.
Scope unions match literal path-component boundaries plus recorded devices.
Search never stats sources or rereads mount information.

Evidence identities are now `SHA256('bytes:'+content_hash)` where known and
`SHA256('path:'+stored_path)` otherwise. Byte identity is established only at
indexing and may therefore change an imported file's result_id once. Copies
merge at file level, carry selected-scope provenance and retain a separately
identified evidence path. Changed versions and unknown-hash files remain distinct.

All regular files can be catalogued without embedding. Filename-only rows have
no chunks/content hash and are searchable with `--names`; content indexing is
bounded separately. Explicit file reads verify the initial size/mtime signature
after reading; a changed file cannot publish that read as a complete new version.
Identical, fully indexed text can reuse stored embeddings when extracted passages
and extractor identity match. No source deletion occurs on an incomplete walk.
