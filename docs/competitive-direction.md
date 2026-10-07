# Searchfu's competitive direction

## Work division

The GitHub candidate uses synthetic tests for public evaluation. The owner later
authorized installation from this repository, EMS integration, named NVMe/archive
scopes and an aggregate-only comparison with the existing local RAG backend.
Private corpora, collection configuration and evaluation content stay outside
source. Ordinary source review is performed directly; local workers serve as
privacy buffers when private material requires them.

## Position

Searchfu should own progressive search scheduling and its evidence contract:
useful early matches, subsequent discoveries, optional more expensive relevance
work, explicit scope/coverage and cancellation. Retrieval backends can be
replaced without replacing that contract or recrawling the source corpus.
This is a design direction, not a proven claim of uniqueness or superiority.

| System | Strength to borrow | Implication for Searchfu |
|---|---|---|
| [QMD](https://github.com/tobi/qmd) | Typed lexical/semantic queries, hybrid rankings, learned reranking, collection context | Closest product comparison; improve how queries and candidate relevance are handled |
| [LEANN](https://github.com/StarTrail-org/LEANN) | Graph navigation and selective embedding recomputation for reduced storage | Borrow rapid candidate discovery; storage savings and latency are separate decisions |
| [txtai](https://github.com/neuml/txtai) | Composable semantic-search and AI workflows | Keep retrieval stages replaceable instead of growing an entire orchestration framework |
| [Recoll](https://www.recoll.org/) | Mature document-format extraction and indexed full-text search | Borrow indexing-time extraction conventions; richer coverage belongs at ingest, never at query time |

## Implemented and tested

The candidate now implements separate lexical/conceptual wording, optional early
HNSW discovery, offline learned reranking with rank blending, stable identities
and optional evidence deltas, plus indexing-time DOCX and text-PDF extraction.
These borrow competitor concepts while preserving Searchfu's progressive contract.
See [experiments](experiments.md) for ablations, costs and rejected defaults.

Keep Searchfu as the progressive controller rather than fork a whole competitor
without evidence of a better fit. HNSW is an optional prototype with considerable
storage and per-search loading costs, not LEANN's compressed graph implementation.
Disk-oriented discovery, OCR and bounded neighboring passage context remain future
work. No off-the-shelf product was installed or benchmarked in this evaluation.
