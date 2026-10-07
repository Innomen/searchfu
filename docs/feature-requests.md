# Searchfu feature requests

Searchfu is the default local discovery tool. Record a concrete limitation or
specialized need whenever another tool is used. Keep private queries, results,
filenames and collection configuration out of this document and public issues.
Use an existing request when possible; record additional safe evidence there.

Each entry should include the operation attempted, observed limitation,
fallback and why it is needed, desired capability, validation and status.
Missing coverage calls for explicit persistent indexing, never search-time
source crawling. A failed search is not proof that the item does not exist.

## SF-001 — Exact developer-source inspection

Operation: inspect or verify exact syntax in already-known application source.
Limitation: indexed discovery does not supply ripgrep's exact regex, line and
source-verification contract. Higher-priority developer instructions require
`rg` first for code/file inspection. Fallback: `rg` confined to authorized
source. Request: consider indexed literal/regex discovery with line provenance
and freshness information; direct verification of current source remains a
specialized operation. Validation: generated code fixtures and edited-source
freshness cases. Status: recorded; no regex parity claimed.

## SF-002 — Archive-scale scoped latency and matched comparison

Operation: retrieve EMS semantic evidence from the adopted large index.
Evidence: the corrected deployed query completed in 24.2708 seconds; concurrent
filename indexing makes this an uncontrolled timing. Earlier small-corpus RAG
comparisons do not measure the adopted deployment. Fallback: retain early
lexical evidence and cancel when sufficient; preserved RAG is a comparison
baseline, not an automatic replacement. Request: improve scoped semantic
latency and run a matched post-adoption comparison, separating cold/warm runs,
first evidence, final latency, cancellation and relevance. Status: open.

## SF-003 — Coverage and exclusion policy

Operation: search material whose content processing is incomplete or intentionally
excluded. Limitation: a filename catalog is not complete text/media coverage;
74 earlier traversal events have unknown causes. Failure classification and
persistent aggregate run history are now implemented, but those old causes
cannot be reconstructed. Fallback: explicit targeted indexing where authorized;
report absent coverage rather than crawling during search. Request: persistent
per-scope exclusion rules, conceptual scope guidance and feature-level coverage.
Validation: synthetic excluded, inaccessible, disappearing and unprocessed items.
Status: open; see archive-direction.md for the broader contract.

## SF-004 — Multimodal retrieval integration

Operation: retrieve by a supplied image/crop or video/audio moment.
Limitation: the active progressive EMS route is text retrieval; legacy CLIP
support is not a complete example-image or timestamped multimodal interface.
Fallback: existing authorized local OCR/transcription tools can create reusable
text sidecars. Request and validation: follow archive-direction.md, preserving
visual representations, source locations, timestamps and versioned coverage.
Status: deferred; text integration remains primary.
