# Agent search protocol

The agent starts retrieval, consumes early evidence while deeper work proceeds,
and cancels if it has enough. Default searches read indexed material exclusively.
The opt-in `--refresh` beta checks up to twelve known plain-text files (64 KiB
each), including a rotating catalog sample, and banks changed content before
retrieving from a fresh snapshot. No source directories are traversed.
Refresh events expose bounded counters/status; failures preserve old evidence.
Cancellation and time budgets are cooperative during GPU admission/encoding.

## Query preparation

Translate the user's story into 3–8 distinctive terms. Keep names, subjects,
identifiers and unusual phrases. Drop conversational framing. Example:
`Toshiba HDMI Nvidia wake recovery`. If there are two plausible descriptions,
add a short `--expand` query instead of joining both stories into one prompt.
Up to eight distinct variants share a semantic scan. This avoids separate
matrix reads for each phrasing. Query length guidance is advisory; language,
names and exact phrases can justify longer queries.

## Background protocol

1. `bash searchfu.sh start "short tags" --expand "alternative tags" --deep`
   returns `job_id`. Deep search is optional and may cost significant time.
2. `bash searchfu.sh poll JOB_ID --after 0` returns events and `next_sequence`.
3. Read keyword evidence as soon as it arrives. Continue reasoning or other
   work while semantic retrieval runs. Poll with the previous `next_sequence`.
4. Compare new snapshots with prior evidence. Results have file-level rank
   fusion, matched chunk IDs, snippets, paths and per-source ranks. Scores are
   relative ranking signals, not confidence estimates.
5. Cancel once the evidence answers the task: `bash searchfu.sh cancel JOB_ID`.
   Continue polling to a terminal status. Cancellation is cooperative; a model
   load/encode or native math call already running must return first.
6. If searching for missing evidence matters, wait for the requested coverage.
   A partial pass or candidate refinement does not justify “nothing else exists.”

A poll returns at most 32 events by default; `--limit` allows 1–100. Do not reuse
an old cursor accidentally, or you will process the same events again. A job's
terminal state does not mean every event has been read: keep polling until no
new events remain. Previous events survive cancellation or failure.

## Stages

| Stage | What it adds | What it cannot establish |
|---|---|---|
| `keyword` | Matches distinctive words in FTS5; no model needed | Synonyms or a topic exhaustively searched |
| `encoding` | Local query vectors are being prepared | New search results |
| `semantic` | Partial, then complete compressed-vector scan plus SQLite tail | Original-vector exact ranking |
| `refined` | Original-vector scores for promising candidates | Recovery of every candidate missed by compression |
| `deep` | Partial, then complete original-vector scan from SQLite | Unindexed/source content or perfect conceptual recall |
| `done` | Requested pipeline completed, with stated coverage | Correctness of the answer |
| `cancelled` | Earlier evidence remains; deeper work stopped or budget exhausted | Completed coverage |
| `failed` | Background job failed; prior events remain readable | Successful deeper stage |

If the compressed cache is absent, semantic retrieval uses stored original
vectors immediately and emits `deep` progress. It never rebuilds the index or
reads the source corpus. A metadata mismatch fails visibly instead of quietly
using a cache from a different index.

`stream` emits the same events as newline-delimited JSON for clients that can
consume live tool output. Many LLM tool runners wait for a process to exit;
those clients should use `start`/`poll`/`cancel` to actually see early results.
`search` remains the blocking compatibility interface.

## Coverage and index health

Coverage describes a SQLite read snapshot, the provided query variants,
path/date/type scope, and the current generated-file exclusion rules. Check
`index_health` for incomplete indexing and prior build/read errors. Files changed
since the last indexing pass are outside this guarantee. Even a completed
original-vector pass cannot prove that the embedding model captured every
relevant concept. Agreement between stages is evidence, not a completeness test.

## Operating constraints

Default searches are read-only with respect to index content. The explicit
`--refresh` option permits bounded transactional read repair through a managed
priority GPU lease; it may add a second retrieval pass. Background state contains
queries and evidence, belongs outside the source project, and is owner-only.
Do not put private results into shared logs. Source access is a separate explicit
operation when the user wants to open a particular result. Only `build`/`update`
walk source roots; cache rebuilds and verification use SQLite embeddings.

One job can search several phrasings together. Avoid launching many full scans
in parallel: shared page cache does not eliminate duplicated computation. Long
read snapshots can retain SQLite WAL pages during concurrent indexing; finish
or cancel unnecessary jobs.

## Optional evidence and ranking stages

`--lex` and `--semantic` each accept repeated variants (up to eight distinct
queries per route). Without overrides, the base query and `--expand` feed both.
Use short distinctive tags for lexical matching and a concise conceptual question
for semantic matching. `--early` adds approximate stored-vector graph candidates
before the complete indexed pass; graph absence/failure skips that stage safely.

Each file has a stable `result_id` derived from its path. This is an identity,
not anonymization: result payloads still contain indexed content and paths and
must stay behind the local privacy boundary. With `--deltas`, intermediate result
events carry `changes`: `added`, `updated`, `removed`, and ordered result IDs.
Apply removals, upsert added/updated objects, then order by `order`. A removal
means absence from the current top set, not deletion of earlier evidence. Terminal
`done`/`cancelled` events include full result snapshots for recovery.

Optional offline CPU reranking reads only retrieved indexed passages, up to four
per candidate file, in bounded batches. `relevance_score` is the learned score;
`ranking_score` is the rank blend used for ordering. Existing `score` retains the
retrieval fusion score. None is a probability or truth guarantee. A terminal
`ranking_coverage: reranked_candidates_only` describes this limited operation;
retrieval `coverage` remains separate. Model failure retains retrieval results.
`--rerank-early` is explicit because another inference pass adds cost.
