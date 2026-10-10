# searchfu

Progressive, offline search for an LLM working over an existing local index.
Return useful evidence immediately, improve it while the agent reads, and stop
when it has enough. Every search also checks a bounded set of known files and
banks changes while indexed retrieval proceeds. Searching helps keep the index
fresh. No source directories are walked; use `--no-refresh` for index-only search.

## The workflow

1. **Keyword results:** distinctive terms find indexed text without a model.
2. **Semantic results:** scan the compressed vector cache in bounded batches,
   delivering intermediate snapshots instead of one long wait. New indexed
   rows not yet in the cache are scored from SQLite.
3. **Refined results:** check promising candidates against their original
   float32 embeddings. This sharpens ordering without another complete pass.
4. **Optional deep results:** `--deep` scores all eligible original embeddings
   in SQLite. It can recover matches lost through compression.

The calling LLM decides when the evidence is sufficient. Searchfu reports work
performed, rather than claiming an answer is correct or a topic exhausted.
Keyword and semantic rankings are combined with file-level reciprocal-rank
fusion. Scores are ranking signals, not probabilities. Rankings may change as
more evidence arrives; previous events remain available to a background job.

Read [PRIVACY.md](PRIVACY.md) before connecting a remote agent or publishing.

## Install and index

Python 3.10+, SQLite with FTS5, Bash and `flock` are required. Install Python
requirements in your preferred virtual environment:

```bash
python3 -m pip install -r requirements.txt
export SEARCHFU_ROOT=/path/to/text-files
export SEARCHFU_DIR=/path/to/private-index
bash searchfu.sh build --allow-model-download
```

MiniLM-L6-v2 is downloaded only when explicitly allowed. Searches run offline.
The wrapper uses CPU and text-only indexing. Supported files include text,
Markdown, source code, JSON, YAML, CSV and subtitle text; DOCX extraction uses the standard library; text PDFs require optional `pypdf`.
Scanned PDFs require separate explicit OCR; legacy binary Office formats are unsupported. The first build walks the supplied roots. `update` walks
them again for change detection, but reads/embeds only changed files. There is
also bounded search-triggered refresh of already cataloged plain text. Signatures use size and modification time, not a
content hash. Huge files are currently read/chunked in memory before encoding.

Developer shell/Python interfaces: explicitly set `SEARCHFU_DIR` to an existing index.
The default now lives outside the source tree, at
`$XDG_DATA_HOME/searchfu` or `~/.local/share/searchfu`. No old data is moved or
rebuilt automatically. Direct Python and shell commands share the same DB
location. `SEARCHFU_DB` overrides the developer interfaces.

The installed `~/.local/bin/searchfu` launcher and EMS adapter pin the database,
ANN matrix, ANN builder input and candidate cache to `~/.local/share/searchfu`.
They ignore inherited index-location overrides, including `XDG_DATA_HOME`; no
backup lookup or fallback exists. A missing primary index fails rather than
selecting another copy. The generic `bash searchfu.sh` and explicit Python DB
arguments remain available for synthetic tests and deliberate migrations.

## Search while the agent works

```bash
# One live stream: each line is a flushed JSON event.
bash searchfu.sh stream "database migration rollback" --expand "schema upgrade recovery"

# Start a job and return immediately with a job_id.
bash searchfu.sh start "database migration rollback" --deep --max-seconds 120

# Use the returned job_id. Advance --after to next_sequence on each poll.
bash searchfu.sh poll JOB_ID --after 0
bash searchfu.sh cancel JOB_ID
bash searchfu.sh poll JOB_ID --after LAST_SEQUENCE
```

`poll` returns events, current status, and `next_sequence`. Events contain a
stage, elapsed time, results when available, and coverage/progress fields.
Intermediate results are provisional. Terminal stages are `done`, `cancelled`
and `failed`; failures retain earlier events. Cancellation preserves delivered
results. Check polls until a terminal status is observed. Ctrl+C/SIGTERM also
cancels `stream`. Checks happen between bounded operations, including matrix
lock waits; model loading/encoding and native math already in flight must
return before cooperative cancellation takes effect.

`--max-seconds` is a cooperative total budget, not a hard realtime deadline.
`--fts` stops after the keyword stage without loading a model. Without a matrix,
semantic search falls back to stored SQLite vectors, with progress. It never
falls back to scanning source files.

Blocking compatibility commands remain available:

```bash
bash searchfu.sh search "migration rollback" --fts
bash searchfu.sh search "migration rollback" --path docs --after 2025-01-01
bash searchfu.sh status --agent
```

## Give it tags, not a story

Start with roughly **3–8 distinctive terms**: names, subjects, error codes,
identifiers, unusual phrases. For example:

- Useful: `Toshiba HDMI Nvidia wake recovery`
- Wordy: `Find the thing from when we had that problem with the television...`

Use `--expand "another short angle"` for a synonym or alternative description.
Up to eight distinct query variants share one semantic scan. No separate
query-writing LLM is launched. Wordy queries receive a guidance field, but are
never silently shortened. Shorter is not always more accurate; preserve the
terms that distinguish the intended subject. `--and` requires every term in
keyword matches only; semantic matches can use other words.

## What “finished” means

- `keyword_index_only`: keyword retrieval finished, no semantic pass.
- `quantized_matrix_and_sqlite_tail`: all compressed cache rows and newer
  indexed vectors were considered; finalist scores were refined at float32.
- `full_precision_candidates_only`: a refinement event, not exhaustive recall.
- `full_precision_indexed_vectors`: every eligible stored original vector was
  scored for every supplied query variant.
- `partial`: stopped or still working; coverage is incomplete.

These refer to a consistent SQLite snapshot and the configured scope/filter.
They do not cover unindexed files, excluded generated files, unreadable inputs,
changes since indexing, or ideas that the embedding model fails to represent.
Index-health fields expose incomplete updates and prior build errors. Agreement
between two rankings is useful evidence, never proof of completeness.

## Storage, precision and performance

SQLite retains chunk text, FTS5 and original float32 vectors. The compressed
matrix is an additional cache, not the whole index. For 25M 384-dimensional
chunks, vectors alone occupy about 35.8 GiB in SQLite; cache embeddings add
8.94 GiB (int8), 4.47 GiB (int4), or 17.9 GiB (fp16), plus IDs and overhead.
All three caches lose some precision. Exhaustive scanning means no graph
pruning, not that compressed rankings exactly match the original vectors.

The scan costs grow with indexed vector count. Cached data avoids some disk
I/O; it does not remove computation. No sub-second large-corpus guarantee is
made. See [performance](docs/performance.md) for measured results and limitations.

Cache operations use only the existing SQLite index:

```bash
python3 ann.py build --dtype int8
python3 ann.py build --full --dtype int4
python3 ann.py verify --n 20
```

Changing dtype requires `--full`, or a separate `SEARCHFU_ANN_DIR`. This rebuilds
a cache from stored embeddings; it does not crawl or re-embed the source drive.
`verify` measures compression recall against original vectors using sampled
stored vectors as queries. It does not measure human relevance.

## Configuration and privacy

| Variable | Default / meaning |
|---|---|
| `SEARCHFU_ROOT` | `.`; roots for indexing only |
| `SEARCHFU_DIR` | XDG data directory `searchfu`; canonical DB and cache |
| `SEARCHFU_DB` | `$SEARCHFU_DIR/drive.sqlite3` |
| `SEARCHFU_ANN_DIR` | `$SEARCHFU_DIR/ann` |
| `SEARCHFU_JOBS_DIR` | `$XDG_STATE_HOME/searchfu/jobs` or `~/.local/state/searchfu/jobs` |
| `SEARCHFU_PY` | `python3` |
| `SEARCHFU_CUDA` | Set to `1` for wrapper GPU encoding |
| `SEARCHFU_WORKERS` | `6`; file-reading workers during indexing |
| `SEARCHFU_BATCH` | `1536`; queued chunks before encoding flush |
| `SEARCHFU_ANN_BLOCK` | `1000000`; cache rows per disk block |
| `SEARCHFU_RESERVE_BYTES` | `8589934592`; free space reserved before writes |

Indexes and job results are private, unencrypted user data. Job directories are
owner-only; query requests are removed on normal worker termination. Results
persist for later polling until the user removes the job directory. Job data
cannot be configured inside the source project. Searchfu has no cloud API;
passing results to a remote LLM is a decision made by its caller. Don't commit
indexes, model caches, job requests or results.

[Agent protocol](docs/ai-protocol.md) · [Format](SPEC.md) ·
[Troubleshooting](docs/troubleshooting.md) · [Borrowed concepts](docs/design.md)

```bash
python3 -m unittest discover -s tests -v
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python3 benchmarks/benchmark.py
```

Tests and benchmarks use generated data only. No GitHub upload or license
assignment is performed by these commands.


### Optional search stages

Use distinctive lexical tags and a concise conceptual question separately:

```bash
bash searchfu.sh stream 'search cancellation' --lex 'cancel partial results' --semantic 'How does cancellation preserve evidence already delivered?' --deltas
bash searchfu.sh candidates-build
bash searchfu.sh stream 'search cancellation' --early --candidate-ef 2000 --deep
```

Install `requirements-optional.txt` for graph discovery, PDF extraction and modern
local cross-encoder support. Graph construction reads stored index vectors only;
it never opens source documents. Graphs are capped at 250,000 chunks by default
and consume substantial RAM and disk. Rebuild explicitly after indexing.
`--early` falls back to the regular indexed search if its optional cache fails.

`--rerank-model /path/to/cached/model` enables offline CPU relevance ranking after
retrieval. No search-time downloads occur. `--rerank-mix 0.25` blends learned and
retrieval order; the appropriate setting depends on the workload and model.
`--rerank-early` adds a second inference pass and is off by default.
All these options are shared by `search`, `stream` and `start`.
`--fts` deliberately ends after lexical results, regardless of optional stages.
See [controlled experiments](docs/experiments.md) for measured benefits and costs.

### Named scopes

Named collections share a canonical index, including nested views and identical
backup copies. Use `--scope workspace`, repeat it for a union, or use `--names`
for stored filename lookup without models. Scope configuration and index data
remain outside the application. [Collection setup and semantics](docs/collections.md)
cover mount boundaries, copy preference and offline backups.

Install from your clone with `bash install.sh`; it creates a dedicated environment
and a `~/.local/bin/searchfu` launcher. Existing compatible system ML packages can
be reused without modifying their environment. Runtime content remains outside
the clone. Installation does not crawl or index anything.

### Rights

Copyright 2026 Innomen. All rights reserved. This source is publicly viewable,
but no open-source license is granted. See [LICENSE](LICENSE).

### Search-driven refresh

`search`, `stream`, `start`, the Python retrieval API and the installed EMS
adapter refresh by default. Use `--no-refresh` or `refresh=False` to opt out.
`--refresh` remains accepted for compatibility.

At the first indexed evidence stage, a separate worker checks up to six result
files and six rotating catalog entries while retrieval continues. Eligible UTF-8
plain text is limited to 64 KiB per file. No directories are walked; new files,
larger files, document extraction and media still need explicit updates.
Signatures detect changes. Symlinks, unavailable mounts, exclusions and unreadable
files preserve old evidence. A nonblocking writer lock avoids competing refreshers.

Changed text uses Archon's shared GPU lease: the LLM stays loaded, with its
context unchanged. Embedding uses batches of at most eight chunks and a 256 MiB
PyTorch allocator cap. CUDA context/library overhead is additional. A synthetic
measurement used 274–322 MiB total process VRAM across batches 1–8; the complete
search/refresh smoke test used 264 MiB with the same resident LLM throughout.
These are measurements, not a universal memory guarantee. Shared admission
requires at least 640 MiB free and refuses active inference, competing leases,
queued work, inhibit and foreground guards. Missing Archon/shared support or
insufficient room skips embedding; it never falls back to an exclusive lease
or bulk CPU embedding. Explicit bulk indexing retains its separate priority lease.
See [scheduler integration](docs/shared-refresh.md) for the required API and patch.

Each file replacement is atomic; existing ANN IDs are never reused. After the
worker exits, retrieval opens a fresh snapshot if anything changed. Early evidence
stays available; refresh failures preserve usable results. Events include bounded
`refresh` counters/status. Retrieval completion does not mean the corpus is fresh.
Keyword/name retrieval itself uses no model, but default refresh may embed changed
text; add `--no-refresh` for model-free index-only operations.

Cancellation owns the worker through exit and GPU memory release. Time budgets
are soft during model loading or an in-flight batch. Model initialization adds
latency: the measured complete synthetic smoke test took about 15.5 seconds.
The worker starts alongside retrieval; updated files can cause a second retrieval
pass. This is bounded incremental maintenance, without directory discovery or
automatic ANN rebuilds on every search.

The installed default database and ANN storage were verified on NVMe, not USB,
on 2026-10-07. Installed consumers ignore inherited index-location overrides; only explicit
developer interfaces can select other locations.

Source rollback instructions and database limits: [docs/rollback.md](docs/rollback.md).

### Desktop GUI and real-use comparisons

Launch **Searchfu** from the application menu, or run `searchfu gui`.
The desktop client has Search, Browse index, Comparison, and Settings tabs.
Search offers hybrid, keyword, filename and deep modes, named-folder filters,
progressive previews, cancellation and opening local files. Browse reads the
stored filename catalog with a literal text filter and 100-row pages; it never
walks source folders. Settings edits folder views over already indexed content;
creating a view does not initiate indexing.

Comparison is enabled by default. Select your existing USB SQLite database in
Settings (and its matrix/candidate folders if they are not sibling `ann/` and
`candidates/` directories). Alternatively, configure it locally with
`searchfu metrics --set-usb-db /absolute/path/to/drive.sqlite3`.
No index is discovered, copied, rebuilt, refreshed or substituted automatically.
Until a target is selected, searches succeed normally and count a missing-target
skip. An absent USB target never creates an empty database. NVMe remains primary.

The CLI, stream, background jobs, desktop client and EMS adapter all use the same
retrieval wrapper. An identical request runs concurrently against the USB index,
with refresh disabled there. One shadow is allowed at a time across processes;
additional requests are counted as busy skips, not queued. The primary never
waits for USB. A detached supervisor owns the read-only child through completion,
cancellation or a hard timeout (default 120 seconds, configurable 0–300; 0 means unlimited).
In-process custom rerankers cannot be cloned and are counted as unsupported.
Use `--no-comparison` for an individual search, or disable it in Settings.

The Comparison tab shows aggregate latency, time to first indexed response,
query encoding/model startup time, index stage time, result counts, result-set
Jaccard overlap, top-result agreement and which target finished sooner. It can
measure logical and allocated database/journal/matrix/candidate footprint and
export or reset metrics. Allocated bytes are filesystem block accounting, not
guaranteed reclaimable space when extents are shared.
`searchfu metrics --agent` is the same payload-free diagnostic surface;
`--footprint` adds artifact sizes, and `--reset` clears counters.
Latency histograms are included in the export. These are concurrent real-use
measurements: cache warmth, model startup, IO contention and differing coverage
can affect the comparison. Index-stage timing excludes query encoding but
includes ranking; after a primary refresh it describes the final snapshot pass.
Total primary latency includes freshness repair. Agreement is not accuracy,
and a low overlap may mean the copies index different material.

Only fixed numeric counters, sums and histograms are retained, separated by
four search modes. There are no per-search rows, timestamps, query hashes,
filenames, snippets, result IDs or scope names. Requests and transient result
identities travel through anonymous local pipes and memory, never log files or
telemetry. Settings live in `XDG_CONFIG_HOME/searchfu/comparison.json` and metrics
in `XDG_STATE_HOME/searchfu/metrics.sqlite3`, both outside source and mode 0600.
The GUI uses local Qt widgets, no web server or third-party assets. It stores no
query history. Indexing remains an explicit CLI operation.

The GUI Search tab includes a local activity console with stage names, elapsed
time and result counts. It continues updating while the first keyword ranking
is running; common terms on a cold large index may take time before results
appear. Activity is not saved and excludes query text and filenames. Settings
shows the active primary database and cache folders. USB comparison requires an
explicit existing database; optional cache overrides otherwise use `ann` and
`candidates` beside that database, with effective paths shown in the form.

Use the GUI **Diagnostics** tab or `searchfu errors --agent` for a privacy-safe
error log. The log stores only fixed component/error categories and counts in
`~/.local/state/searchfu/errors.sqlite3`. It captures GUI/worker failures, invalid
comparison settings, missing USB targets and shadow failures/timeouts. No query,
filename, result, raw exception or traceback is retained; old failures cannot
be recovered retroactively. A logging failure never interrupts search.

AI/local automation can control the same desktop through a same-user Unix
socket: `searchfu gui-control status`, `reload`, `cancel`, `show`, or `search`.
Search text comes from stdin, never command-line arguments; optional `--scope`
and `--mode` choose the controls. Replies expose only accepted/running state,
fixed phases/outcomes, elapsed seconds and result counts. They never echo the
query, scope, paths or snippets. Reload waits for owned workers to exit before
replacing the GUI process. One GUI owns the socket; repeated GUI launches show
the existing window. Socket/lock permissions are 0600 and no TCP port is opened.

Comparison now shows active ownership, every mode including zero counts, and
standalone primary timing even if USB pairing fails. Search budget exhaustion
is distinct from cancellation. Paired latency remains separate from standalone
attempt statistics; neither implies content relevance or source coverage.

Older USB databases are searched through temporary compatibility views;
search never migrates or writes the USB database. Keyword ranking uses the
native FTS rank ordering to avoid sorting joined matches before first results.

To explicitly upgrade the configured USB catalog without a content recrawl,
run `searchfu metadata-upgrade --comparison-target`. It adds missing metadata
fields and stats already indexed paths in resumable batches, preserving stored
text, embeddings and cache identity. Reports contain only counts and timings.
Unavailable paths remain unknown and can still prevent a complete folder-pinned
comparison. This command writes metadata; ordinary shadow searches remain
read-only. It does not rebuild the full-text index or address USB query latency.

### Continuous experiment retention

Settings → Search time limit accepts 0 (No time limit) for both primary GUI searches and USB comparisons. Cancel remains available while the USB comparison runs after primary results finish. Partial observations are banked independently of completed pairs: result counts, elapsed time, time to first result, and USB CPU seconds and physical read bytes. USB observations persist every five seconds, including while retrieval has not emitted results. Cancellation and failures preserve these observations; they do not count as completed pairs.

GUI and USB retrieval events also remain in private local evidence journals under XDG_STATE_HOME/searchfu/evidence (directories 0700, files 0600). These contain private results and must never be read by remote agents. They are separate from the aggregate-only metrics and diagnostics; no query history is added to those surfaces.

The intended deployment is one logical computer-wide index with NVMe as its
authoritative storage and USB as a replica. Independently existing databases
may have different coverage; identical search text does not make their timing
a controlled drive comparison. To check stored inventories without exporting
filenames, use `searchfu inventory-compare --comparison-target`. Its shared-path
counts do not establish content equality.

Use `--counts-only` for a quick inventory/size check or `--plan-only` for closed
query-plan booleans. Overlap reads the USB inventory and probes the NVMe path
index. Optional exact chunk counts have a separate 15-second diagnostic budget;
`null` means unavailable, and the chunk-ID upper bound is not a row count.

Use `searchfu replicate --comparison-target --wait` to explicitly copy the
NVMe SQLite snapshot and matching caches to the configured USB target. It waits
for an active comparison to finish, excludes cooperating index/cache writers,
includes committed WAL data and stages the full copy before replacement.
Previous USB artifacts remain beside the target for rollback. This needs free
space for the entire new copy plus an 8 GiB reserve; it neither crawls source
files nor re-embeds. Failed transfers retain the old usable target. Copy and
publication errors trigger rollback; abrupt power loss during the short
multi-artifact publication window requires local recovery from retained copies.

The Comparison screen distinguishes verified matching snapshots from different
or unverified ones. Source updates invalidate replica freshness; replication is
explicit, not automatically triggered by every small query refresh. Matched
snapshot status also requires refresh disabled for both measured searches.
