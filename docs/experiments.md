# Controlled synthetic experiments

All files, passages, queries and targets were fabricated. No real corpus was
searched, enumerated or used to choose targets. These tests exercise implementation
and tradeoffs; they do not establish general relevance or product superiority.

## Decisions

| Change | Result | Decision |
|---|---|---|
| Separate lexical tags and conceptual wording | Target appeared in top three for 6/8 authored cases instead of 3/8 | Keep; caller supplies wording, no automatic expansion model |
| Early HNSW, 30,000 chunks, ef=500 | Time to 80% reference file recall: 47.1 → 23.5 ms | Keep optional |
| Early HNSW, 100,000 chunks, ef=500 | Time to 80% recall: 114.8 → 156.9 ms | Reject as universal setting |
| Early HNSW, 100,000 chunks, ef=2000 | Time to 80% recall: 114.8 → 67.7 ms | Useful optional setting; measure on intended scale |
| Delta delivery | Same reconstructed results with smaller serialized payload | Keep optional; snapshots remain default |
| Stronger learned reranker with tags alone | No target recall improvement; added inference cost | Reject as default |
| Separate wording plus stronger reranker | Pure learned ranking: 1/8 top-one and 6/8 top-three; 25% learned blend: 7/8 top-three | Keep optional, no universal model/mix |
| Automatic reranking of both early and final pools | Extra inference nearly doubled illustrative runtime | Reject automatic duplication; explicit --rerank-early only |
| DOCX and text-PDF extraction | Synthetic extraction and stored provenance tests pass | Keep at explicit indexing only |

The tiny reranker trial improved tags-only top-three recovery but reduced it when
combined with separate wording. Improvements are not automatically additive.
The stronger model is `cross-encoder/ms-marco-MiniLM-L6-v2`; the tiny trial used
`cross-encoder/ms-marco-TinyBERT-L2-v2`. Neither is a factual correctness checker.
Distractors include topical but wrong answers and tag glossaries, deliberately
making this small suite difficult. Eight authored cases are too few for a default
relevance-model recommendation. The stronger model added about 240 ms of warm
CPU inference to the typed-query case; first lexical evidence still arrives first.

At 100,000 chunks, the graph occupied 168.5 MB and built in 9.4 seconds with eight
threads. Its 67.7 ms first semantic delivery was slower than the regular first
batch's 13.6 ms, but contained 86.7% rather than 15% of reference top files.
Deep completion was 674.5 rather than 586 ms. Keep this distinction visible:
earlier useful evidence is the benefit; earliest response and total runtime suffer.
The graph loads per search; no resident service or disk-backed graph is supplied.

## Reproduction and records

`benchmarks/experiments.py` creates temporary indexed fixtures and performs paired
ablations, eight relevance combinations, blended rankings and delta reconstruction.
Use `--rows 30000` or `--rows 100000` and `--reranker /path/to/offline/model`.
Public benchmark models must be cached explicitly beforehand. No search-time
network access is needed. Set `OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1` for the
recorded scoring configuration; graph construction explicitly uses eight threads.
CPU, warm-cache results include graph loading but are not cold external-disk tests.

- [30,000-chunk final results](experiment-results.json)
- [100,000-chunk final results](experiment-results-100k.json)
- [Initial tiny-model trial](experiment-results-tinybert.json) includes the earlier automatic double-reranking behavior, subsequently removed.

Vector recall compares with exhaustive stored original float32 vectors, not human
relevance. Relevance metrics compare against authored targets. Full deep passes
recovered all reference files in these trials. Independent optional stages do
not replace the full indexed pass or certify that every useful real-world source
exists in the index. Cancellation preserves delivered evidence without claiming
complete coverage.
