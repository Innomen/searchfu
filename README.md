# searchfu

Progressive, offline search for an LLM working over an existing local index.
Return useful evidence immediately, improve it while the agent reads, and stop
when it has enough. Search never walks or opens the source corpus: paths,
snippets and embeddings are read from the index.

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
no search-triggered update. Signatures use size and modification time, not a
content hash. Huge files are currently read/chunked in memory before encoding.

Existing installations: explicitly set `SEARCHFU_DIR` to your existing index.
The default now lives outside the source tree, at
`$XDG_DATA_HOME/searchfu` or `~/.local/share/searchfu`. No old data is moved or
rebuilt automatically. Direct Python and shell commands share the same DB
location. `SEARCHFU_DB` overrides it.

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
