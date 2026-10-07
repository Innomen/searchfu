# Named collections and overlapping copies

A collection is a persistent named view over stored absolute paths in one
canonical SQLite index. Configuration lives outside source at
`$XDG_CONFIG_HOME/searchfu/collections.json` (or `SEARCHFU_COLLECTIONS`).
The index and vector caches may live on fast local storage while source roots
include slower disks. Search reads the index only, even if a source is absent.

```bash
searchfu scope add workspace /example/workspace --priority 10
searchfu scope add local /example/local --priority 20 --one-filesystem
searchfu scope add archive /example/archive --priority 30 --one-filesystem
searchfu scope build workspace
searchfu scope build local archive
searchfu stream 'cancel evidence' --scope workspace
searchfu stream 'cancel evidence' --scope workspace --scope archive --deep
searchfu search 'diagram.svg' --scope local --names
searchfu scope list --agent
```

Widen by starting another search with additional scopes. Depth and scope are
independent: `--deep` increases precision within the selected union; repeated
`--scope` widens that union. Cancellation/polling retain their existing contract.
No implicit root traversal, mounting, refresh or widening occurs during search.
Unknown/offline scopes fail explicitly rather than silently searching elsewhere.
Scope names, roots and result provenance are local private state.

Nested views share indexed file rows. Explicit indexing hashes original bytes
of supported content files with SHA-256. Identical files return one result with
`copies`; changed copies remain separate. Lowest configured priority chooses the
preferred working path, then newer modification time. `evidence_path` identifies
the actual passage source. Copy provenance is constrained to the selected scopes.
Repeated copies do not sum relevance scores. Files imported without byte hashes
remain separate until explicit source indexing establishes identity. SHA-256
identity is not anonymization, version lineage, or proof of online availability.

All regular, non-symlink files get filename metadata; supported text files also
get indexed passages. `--names` uses literal substrings over stored paths and
loads no model. Unsupported formats and files above `SEARCHFU_MAX_TEXT_BYTES`
(default 64 MiB) remain filename-only. No binary or huge-file content fallback
occurs during query. Generated/install trees remain excluded. Filename lookup
currently scans stored path metadata; it is not a specialized filename trigram
index, so very large catalogs can benefit from a narrow scope and search budget.

`--one-filesystem` records allowed device identities during explicit configuration
and indexing. Linux Btrfs subvolumes can have distinct st_dev values: the local
mount helper includes mounts of the same source, and partitions of the same
NVMe when the root source is NVMe. Queries use recorded device IDs only.
If the root's identity changes, refresh its scope configuration explicitly.

Unplugged backups are excluded unless explicitly added. `scope add ... --offline`
records a disabled scope without touching its root. A searchable source going
away does not invalidate stored evidence; results make no availability check.
Only explicit indexing tests source existence. An unavailable root is never
pruned. Root-level custom exclusions, runtime caches and model caches are skipped.

For existing MiniLM RAG data, `rag_import.import_index(source,root,db)` imports
stored JSON/NumPy data without source traversal, unsafe pickle loading or model
inference. It validates model, alignment, paths and finite vectors and skips
already indexed files. Import does not establish source freshness or byte hashes.
Bulk source indexing can opt into `SEARCHFU_CUDA=1` with Archon; GPU ownership is
acquired lazily only when changed chunks need embeddings, renewed during long
runs and released afterward. CPU search remains independent of the local brain.

RAG import may stat only the filenames already present in stored metadata to
record filesystem identity. It never enumerates source directories or re-embeds.
Indexing skips special files such as FIFOs and uses a temporary SQLite table
for traversal bookkeeping so catalog size does not require a Python path set.

Content collection builds also update the compact vector cache from stored
SQLite vectors. Filename-only builds skip that step and load no model.
