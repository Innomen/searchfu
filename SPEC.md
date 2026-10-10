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

## Desktop client and comparison boundary

`gui.py` is a native Qt client; `searchfu gui` and `searchfu.desktop` share the
installed pinned primary launcher. Queries are passed to a cancellable search
subprocess over stdin. Browse operates on read-only SQLite keyset pages in a
worker thread. Folder-view configuration uses the existing collection contract.

`retrieval.retrieve` wraps the existing primary retrieval/freshness generator.
`comparison.py` supervises one read-only USB shadow per user, bounded by a
configurable hard timeout, or no deadline when configured as 0. The target is explicit local configuration, never a
primary fallback or an index-discovery heuristic. Default enablement takes effect
once a target is configured. A missing target is a counted skip, not a search
failure. A primary terminal event is delivered without waiting for comparison;
the supervisor completes aggregate accounting independently. Closing/cancelling
a primary aborts the shadow; pipe EOF also aborts it.

Comparison persistence is one `counters(mode, metric, value)` table. Keys and
export values are checked against a closed vocabulary; no query, path, identity,
scope, exact timestamp or per-search record may be added. Query encoding is timed
separately to expose cold shadow model startup. Index timings include ranking
and describe the final snapshot, while end-to-end latency includes refresh.
Synthetic tests cover real subprocess pairing, read-only preservation, closed
export, timeout/child exit, cancellation, missing targets, opt-out, cross-process
admission, no traversal, CLI arguments and an offscreen actual GUI search/browse.

Generic synthetic/migration databases cannot enter live comparison merely by
setting SEARCHFU_DB. Eligibility requires the installed primary, or explicit
comparison-config AND metrics-path overrides together for isolated tests.
This gate prevents old CLI fixtures from reading user settings or contaminating
real-use counters. Test the negative boundary, not only positive shadow pairing.


Desktop progress contract (2026-10-10): the worker reports startup and the
initial filename/keyword scan before entering retrieval. The GUI independently
updates elapsed time while SQL has not yielded; it never invents a completion
percentage. Its bounded, unsaved activity console uses fixed phase names and
counts only, never raw stderr, queries, paths or snippets. Worker exit and
cancellation remain terminal acknowledgements, not submission acknowledgements.
Settings displays the authoritative primary path family read-only, explicitly
marks a missing USB target, and shows effective derived USB cache paths alongside
optional overrides. Never discover or guess a comparison database. Synthetic
worker and native GUI gates cover pre-SQL notification, stalled-stage status,
terminal console state and primary path display. An early notification does
not establish faster retrieval; archive-scale cold/common-term FTS remains a
separate performance limitation.

Comparison dashboard contract: an always-visible banner distinguishes disabled,
unconfigured USB, unavailable USB and enabled states. Unconfigured searches
show the aggregate missing-target count. Never present missing comparison output
as a running benchmark. The native synthetic gate checks this blocker message.

Diagnostic contract: diagnostics.py is the sole fixed-vocabulary error sink
and validated export. Counters are bounded by the category vocabulary; no
per-search records, timestamps or free-form strings. GUI callback/startup,
search worker and comparison failure paths use it. Qt exception hooks must
suppress raw tracebacks. Synthetic fixtures validate hostile exception objects,
unknown stored data rejection, permissions, nonmutating reads and public shell
export. User-selected USB locations persist only in private comparison settings;
never bake personal mount paths into source or developer documentation.

AI-first desktop control contract (2026-10-10): gui_control.py owns an
authenticated-by-filesystem same-user Unix socket and singleton lock. Qt handles
commands on its UI thread. Allowlist commands/arguments; bounded requests and
closed sanitized replies. Status/search/show/cancel/reload have public shell
routes; search text is stdin-only. Reload closes owned workers before exec and
server reacquisition. No result serialization through this control boundary.
Comparison progress is an aggregate supervisor-lock probe; dashboard renders
all mode buckets and distinguishes readiness from active ownership. Primary
completion/cancellation/budget exhaustion receives independent numeric timing
even if no pair completes. Historical generic cancellations cannot be reclassified.
Synthetic public-route gates cover actual GUI-controlled search, payload-free
status, singleton permissions, invalid commands, primary timeouts and active
lock reporting. The first upgrade from a GUI predating this endpoint requires
an explicit restart; subsequent upgrades use the controlled reload route.

Legacy USB reader contract: missing optional file metadata and index-state tables
are supplied by TEMP views on a mode=ro connection, never a persisted migration.
Unknown filesystem identity stays NULL and cannot pass a pinned-device filter.
Shadow reads never create a matrix lock; absent legacy locks select the indexed
vector path. FTS uses the native rank ordering with explicit bm25 mapping so
joined matches do not require a temporary sorting tree before initial results.
Synthetic gates compare ranking scores and assert unchanged legacy database
bytes. Initial-result timing requires a nonempty result event; zero denotes no
observed result, not elapsed time to an empty cancellation.
Comparison admission checks legacy filesystem metadata before launching a pinned
scope. Unsupported scopes produce a fixed outcome and visible explanation,
never a zero-hit "USB faster" measurement. Do not silently broaden scope or
rebuild the read-only USB copy; an explicit unscoped search remains supported.
Keyword pagination owns one ranked cursor and closes it after enough distinct
eligible files. Never reissue rank queries with increasing OFFSET: duplicate
chunks from long documents otherwise turn file deduplication into repeated
ranking work. The long-document gate requires multiple chunk pages while
issuing only one ranked query and returning both distinct files.

Installed validation limitation (2026-10-10): owner-authorized keyword control
searches completed on NVMe with 12 results in about 1.2 seconds after the cursor
fix. The legacy USB copy still reached its 300-second hard limit, including an
unscoped retry, and produced no completed pair. Folder-pinned scope admission is
also incompatible with its missing filesystem metadata. Dashboard/control
repair is verified; USB performance recovery is not. Do not treat synthetic
test success, partial primary results, or a zero-hit unsupported scope as proof
of a valid paired benchmark. Search must not silently rewrite the USB index to
resolve these limitations. Actual attempts and their failures remain counted.

Legacy schema upgrades are distinct from corpus recrawls (2026-10-10).
Missing optional columns and bookkeeping can be migrated while retaining stored
chunks, embeddings and FTS data. Filesystem-pinned scopes additionally require
valid source-device metadata: adding a NULL column alone does not establish
coverage. An explicit metadata-only repair can stat already cataloged paths
without discovering directories, extracting content or running embeddings.
Unknown hashes must remain unknown rather than be fabricated from chunk text.
Preserve or deliberately reconcile ANN identity when adding index_state;
creating a new identity must not accidentally invalidate a reusable cache.
Index creation/backfills can incur database I/O, but do not require source
re-extraction. This compatibility work does not establish a remedy for the
separate unscoped USB search timeout. Never prescribe a full recrawl merely
because a legacy index lacks newer optional metadata.

`searchfu metadata-upgrade --comparison-target` is the explicit metadata-only
operation. It preserves an existing ANN identity, initializes chunk high-water
tracking, adds optional columns, and commits known-path device checks in bounded
batches. Unknown paths remain NULL; hashes remain unknown. Comparison admission
rejects migrated pinned scopes with unknown rows within the selected roots.
Rerunning skips resolved rows. Writer/cache/comparison locks prevent overlap.
No text extraction, embedding, FTS rebuild or matrix generation occurs.

Installed metadata validation (2026-10-10): the explicit USB upgrade checked
705,720 catalog files in 732 seconds, updated device metadata for 688,121,
and retained 17,599 unavailable paths as unknown. Existing cache identity was
preserved. Archive keyword retest returned 12 primary results in 1.84 seconds,
but comparison correctly rejected that scope because unknown archive rows
remain. A successful schema operation is not complete filesystem coverage or
proof of USB retrieval performance. Do not delete stored evidence or guess
devices merely to make the comparison pass.
The subsequent unscoped keyword retest returned 12 primary results in 0.86
seconds, while the upgraded USB index again reached the 300-second timeout.
The supervisor exited and active ownership cleared; no valid paired benchmark
was produced. Metadata migration therefore did not resolve USB search latency.

## Continuous experiment contract

Zero search time limit disables both the GUI primary retrieval deadline and the USB supervisor deadline. Nonzero limits retain hard child termination. The GUI cancel action must cancel an active USB comparison even after the primary worker exits. Progressive evidence is retained locally in private append-only event journals and latest snapshots, outside source, with 0700 directories and 0600 files. It is never part of an agent response.

Closed numeric experiment counters checkpoint monotonic elapsed time, first-result time, peak result counts and event counts independently of final pairing. USB supervision checkpoints its owned child CPU seconds and physical read bytes every five seconds. Terminal classification distinguishes completion, cancellation, timeout and failure, retaining the latest partial count. Repeated snapshots must not double-count monotonic observations. Completed-pair metrics remain separate. Synthetic gates cover partial retention after hard timeout and successful completion past the former deadline with unlimited configured.

## One index, two storage copies (2026-10-10)

The owner intends one logical computer-wide index with an authoritative NVMe
copy and a USB replica. Existing unmatched inventories are not a controlled
storage benchmark. `inventory-compare --comparison-target` locally joins stored
paths and returns only shared/unique counts, files/chunks and
database/free-page bytes; it does not claim shared paths have equal contents.

Explicit `replicate --comparison-target --wait` holds the comparison lock after
the preceding experiment completes and writer/cache locks while taking a
SQLite backup plus its matching caches. It stages artifacts, checks copied page
count/cache sizes/index identity, fsyncs, retains existing artifacts and
publishes under the lock, rolling back ordinary errors. Empty WAL creation by
a read-only open is not a source-content change. A private target manifest
records source/target file stamps; changed databases or cache metadata invalidate
freshness. Matched-snapshot counters require verified freshness at launch and
finish plus primary refresh disabled. Missing markers and historical pairs are
unverified; they remain experimental evidence, never retroactively matched.
