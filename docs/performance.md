# Performance

The old alpha's unsubstantiated timings were removed. “Warm” does not imply
sub-second search at arbitrary corpus size, and compression precision is not a
semantic quality scale.

Run the synthetic benchmark with:

```bash
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python3 benchmarks/benchmark.py --rows 30000 --files 3000 --queries 5
```

It creates and deletes a temporary synthetic index. Source paths do not exist;
a traversal guard fails any attempted source walk. Retrieval timing injects
known query vectors, so model load and encoding do not distort backend timing.
If MiniLM is cached, a separate offline measurement compares short-tag and long
narrative encoding. Optional `hnswlib` measures an approximate backend against
the same vectors; it does not benchmark QMD or LEANN as complete applications.

The measurements include time to first keyword results, first semantic
snapshot, finalist refinement, optional complete original-vector coverage,
compression recall, keyword query length, cancellation, and storage. The
reference is the original vector ranking, not human relevance judgments. Small
warm synthetic results cannot be extrapolated to a 25-million-chunk cold index
on a USB drive. A real-corpus benchmark requires separately authorized content
access; no personal corpus was used for this cleanup.

## Storage arithmetic

For N chunks with 384 dimensions:

| Representation | Embedding bytes | At 25M chunks |
|---|---:|---:|
| Original float32 in SQLite | N × 384 × 4 | 35.76 GiB |
| int4 cache | N × 384 / 2 | 4.47 GiB |
| int8 cache | N × 384 | 8.94 GiB |
| fp16 cache | N × 384 × 2 | 17.88 GiB |

Add source text, FTS, SQLite/WAL overhead, metadata and chunk IDs. Cache IDs are
stored both per block and concatenated, adding roughly N × 16 bytes. These are
format calculations, not measurements of a deployed index.

## Cost choices

- FTS retrieves indexed word matches without loading an embedding model.
- The semantic cache pass is linear in cached rows. Smaller precision reduces
  bytes read but adds decoding; it is not guaranteed to be faster.
- A few query variants share a pass, avoiding repeated disk reads, though each
  adds scoring work.
- Refinement reads only promising original vectors.
- Deep search reads all eligible original vectors; cancel it if unnecessary.
- Graph indexes can deliver faster early neighbors at the cost of approximate
  recall and a different RAM/storage profile. Backend-only warm timings omit
  graph loading, model startup, filtering and snippet retrieval.

See [benchmark-results.json](benchmark-results.json) for the recorded run and
[design](design.md) for the interpretation of competing approaches.

## Recorded synthetic run

30,000 chunks, 3,000 files, five vector queries, warm CPU-only execution:

| Measurement | Median |
|---|---:|
| Matching keyword results, three tags | 63.6 ms |
| Matching keyword results, 34-word narrative | 109.3 ms |
| First int8 semantic snapshot | 13.1 ms |
| int8 pipeline through finalist refinement | 48.2 ms |
| int8 pipeline including full original-vector scan | 164.8 ms |
| Cancellation immediately after keyword delivery | 0.103 ms |

Semantic timings use unmatched keyword queries and injected vectors; matching
keyword times are a separate experiment. Cached CPU MiniLM encoding measured
6.8 ms for tags and 11.9 ms for narrative, excluding model startup.

The original blocking path, using the fp16 cache and injected queries, took
18.6 ms. The new fp16 path finished refinement in 41.1 ms: progressive delivery
and extra verification cost work, even when they improve caller control.

At file recall@12 against original vectors, int4 scored 83.3%, int8 98.3%,
and fp16 100%. Finalist refinement recovered 100% on these five queries;
that is not a guarantee for unseen queries. Only a complete original-vector
scan removes candidate-selection omissions for the selected vector queries.

The optional HNSW backend reached 98.3% recall in 5.3 ms with ef=2000,
versus 65% in 1.0 ms with ef=50. It needed 22.2 ms to load from warm disk,
13.3 seconds to build, and a 50.7 MB index; the int8 cache was 12.0 MB.
HNSW is a promising future early-result tier, with an explicit speed/recall
tradeoff. This measurement does not justify replacing Searchfu with a whole
competing application. Query ef is at least the requested candidate count
(180 here), even when the configured value is lower.
