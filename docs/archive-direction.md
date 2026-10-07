# Searchfu archive direction

Long-term direction, recorded 2026-10-06. This is architectural guidance, not authorization to implement every feature now. Finish and measure text retrieval first. Inspect existing components before proposing replacements.

Target archives can span decades and terabytes of heterogeneous images, screenshots, photographs, documents, movies, TV, audio and archived members. Most indexed material changes rarely. Preserve valid records and embeddings; changed or missing coverage should drive new work rather than rebuilding everything.

## Durable work and local privacy

Searching retrieves existing indexes and must never crawl source directories. Missing processing may be requested as explicit, bounded indexing of known source records, with results persisted for subsequent searches. Extraction, decoding, interpretation and exploration should leave reusable structure. All private media, query examples, feedback and derived indexes stay local, outside the public repository. Provider-facing tools expose only approved, validated aggregates.

Coverage must distinguish inventory from processing, and no match from incomplete coverage. Resumable jobs record feature family, model and preprocessing versions, completion, failure category and invalidation reason. A new model does not silently reinterpret or destroy old vectors. Query encoders must match stored feature versions. Existing valid text records remain usable during migration.

## Sources, locations and derived evidence

Separate an item's identity from its individual locations and derived records. Preserve legacy identifiers while evolving this contract. An item can have multiple files or archive-member locations; a feature can describe the whole item, an image region or a timed segment. Each result needs recoverable provenance: file location, containing archive and member path where applicable, crop coordinates or start/end timestamps, and the processing version that produced the evidence.

Cache derivatives using source identity/content fingerprint and processing configuration. Archive extraction can be retained or reproducibly regenerated; it must not require recreating indexed records for every search. Exact identity, modified variants and visual resemblance are separate relationships. Group duplicates for browsing while retaining every location; duplicate copies must not crowd out distinct discoveries.

## Visual retrieval and interaction

Store direct learned visual representations. OCR and captions supplement them rather than define everything that can be found. Support supplied photographs, crops, sketches and other examples without requiring verbal object identification. Direct text queries remain useful alongside example queries.

An optional local generator may translate a description into a proposed visual example. The user approves or adjusts it before retrieval. Invented details must remain accountable to the actual intent rather than silently become requirements. The interaction can select a region, ignore background, emphasize shape/color/texture/layout, add viewpoints and mark closer/farther examples. Persist useful feedback with its scope and feature version; do not turn one query's preference into a universal rule.

Video results describe searchable moments and link to the original timestamp. Representative frames, shot boundaries or periodic samples are implementation options, not commitments. Finer inspection of a promising interval enriches its durable record. Audio follows later: timestamped transcripts first, potentially direct sound representations thereafter. Text, appearance, speech and sound reach the same source records.

## Exploration

Offer browsable visual neighborhoods, representative examples and counts linked to dates, folders, OCR and text evidence. Preserve several derived views: resemblance, chronology, source context and other relationships. No single projection is an authoritative map of the archive. Exploration and useful selections should leave reusable structure.

## Existing components to reuse

Searchfu already stores normalized CLIP whole-image embeddings and can compare a text projection against them in its legacy image route. This is a starting point for direct visual retrieval, not a complete example-query interface: the active progressive EMS route is text retrieval. Benchmark any replacement against retained vectors before choosing one.

The local OCR ingestion tool produces local OCR markdown sidecars and avoids overwriting existing outputs. The local transcription tool decodes audio and produces Whisper transcripts, word-timed segments, SRT/VTT and markdown sidecars; its fixups route can reuse transcription. Connect these derivatives to source/segment identities rather than repeatedly OCRing or transcribing. Existing sidecars are useful searchable evidence, but do not replace direct visual features.

Current SQLite files/chunks, FTS, retained embeddings, appendable matrix cache, collection scopes, progressive result events and cancellation are reusable foundations. Scope selection must also reduce retrieval work: filtering results from an entire archive is not enough. A substantially smaller filtered corpus now uses its indexed full-precision vectors instead of scoring the global matrix; the inventory-to-chunk route avoids reading unrelated vector blobs. Small-corpus timings are not evidence of archive-scale scoped performance; benchmark adopted indexes independently.

## Small decisions now; work that can wait

Now: explicit known-file indexing can refresh changed documents without a directory crawl and does not prune sibling records. Preserve backward compatibility, separate public code from local data, retain recoverable source locators, document feature-version boundaries, make coverage explicit, and measure scoped text queries against the adopted index. Extend metadata additively when a concrete producer/consumer needs it; avoid speculative empty schemas.

Later: choose regional embeddings and visual indexes, expose example/crop queries, implement feedback and neighborhood browsing, select video sampling/refinement policies, integrate archive-member extraction, add direct audio similarity and optional image generation. No bulk visual/video/audio indexing or model replacement is implied by this document.

Future exclusion policies should offer a readable gitignore-like interface per
collection. Existing exclusions are explicit directory prefixes, not a complete
gitignore parser. Keep intentionally excluded material distinct from failed
attempts and unprocessed features; exclusions are coverage policy, not errors.
Do not assume excluded content has been searched. Apply these rules during
indexing; retrieval continues to use stored inventory without crawling.

Scopes may also be conceptual: a nearby AI can compile user intent into saved
rules and indexed classifications using existing metadata. Users need not
enumerate every included or excluded path. Preserve intent, rule versions and
reviewable decisions; new local inspection produces reusable indexing evidence.
Retrieval applies stored scope knowledge without crawling or repeating that
classification for every search. Private evidence stays local. This remains
architectural guidance rather than an implemented conceptual-scope interface.
