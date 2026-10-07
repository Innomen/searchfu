# Design: evidence arrives before the search is finished

The user's requirement is an agent workflow, not simply a choice of embedding
precision. An LLM often starts a search, waits for the tool, then reasons over
results. Searchfu should let it reason over immediate evidence while deeper
retrieval runs, with an explicit way to stop that work. Search must never walk
the source drive. Opening a chosen file and indexing are separate operations.

## First impressions and investigation

Early matches can suggest another short query or a line of thought while
remaining batches run. The caller can poll, pursue that lead, or cancel and
use the evidence already delivered. Treat this like a first impression that
can change, not a confidence score. Missing early matches do not prove absence.
Background jobs retain event history so later ranking changes do not erase
what inspired the caller. Starting a new query is explicit; Searchfu does not
launch an unbounded autonomous chain of searches.

See [competitive direction](competitive-direction.md) for the comparison and ordered improvements.

## Concepts borrowed from existing systems

[QMD](https://github.com/tobi/qmd) combines keyword/semantic retrieval, query
expansion, reciprocal-rank fusion and local reranking. Useful lessons:

- Combine ordered lists instead of adding incomparable keyword/cosine scores.
  Searchfu now uses RRF at file level (`sum(1/(60+rank))`).
- Search more than one wording. Searchfu lets the calling LLM supply short
  alternatives and scores them together in one cache pass. No extra model is
  needed to rewrite a question the caller already understands.
- Spend additional effort on finalists. Searchfu now recomputes their semantic
  scores from stored float32 vectors. This corrects compression distortion; it
  is not QMD's learned relevance reranker. A learned reranker may improve
  ordering later, but it cannot discover a file absent from its candidate pool.

[LEANN](https://github.com/StarTrail-org/LEANN) and its HNSW/DiskANN backends
illustrate a different useful idea: jump to likely semantic neighbors instead
of scanning every vector. That can make an excellent early semantic tier. The
tradeoff is that graph search may miss useful neighbors, and its storage/RAM
requirements differ from a compressed sequential cache. LEANN's selective
embedding recomputation targets storage, whereas an ordinary HNSW index retains
vectors. These are not interchangeable memory profiles.

Searchfu's benchmark includes an optional installed `hnswlib` backend against
the same generated vectors. It compares speed and recall, not full competitor
applications. Neither QMD nor LEANN was installed or benchmarked as a product.
The current implementation does not claim an ANN tier exists: it emits results
after bounded slices of the existing cache. Adopting a disk-oriented fast tier
should preserve the canonical SQLite index and consume stored embeddings; it
must never require a source crawl just to search or build a different cache.

## Why int4 → int8 → fp16 is not the main ladder

All three scan the same semantic representation of the same corpus. Higher
precision can fix close rankings, but does not add new document extraction or
new meanings. It can repeat nearly all the expensive work for a small gain.

The implemented ladder instead changes the work performed:
keyword evidence → progressive semantic discovery → inexpensive finalist
refinement → optional exhaustive original-vector pass. Compression is a cache
choice. The original vectors are retained so new backends can be built from the
index without crawling or re-embedding the source drive.

## Hard contracts

- Only explicit indexing walks source roots. Search, polling, cancellation,
  matrix building and compression verification never do.
- Early evidence is delivered before loading query models.
- Cancellation/budget exhaustion retains delivered evidence and never mutates
  the canonical index. Checks occur between bounded operations.
- Each event describes whether the work is partial, finalist-only or complete
  for the requested index scope. No rank agreement is treated as proof.
- Background state stays outside source control and uses owner-only files.
- New caches are bound to the canonical DB identity; wrong-index caches fail.
- Format/filter changes cannot silently append incompatible cache blocks.
- Failed cache rebuilds must not leave metadata claiming a complete generation.

The user clarified these requirements on 2026-10-06. Regression tests exercise
the public shell routes, synthetic index paths whose source files do not exist,
progressive delivery, cancellation, budget limits, stale/deleted cache rows,
cache identity, rebuild failure, and original-vector verification.
