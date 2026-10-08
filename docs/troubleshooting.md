# Troubleshooting

## Missing or empty index

Run `bash searchfu.sh status --agent`. This reports live chunk/file counts,
index bytes and build health without paths or text. A missing DB is an error;
search never creates it; default refresh only repairs cataloged files. Set `SEARCHFU_DIR`
to the existing index before using an older installation. The new default is
outside the source tree; no existing index was moved. Installed launcher/EMS
consumers pin `~/.local/share/searchfu` and ignore index-location environment
overrides. They never discover a backup index. Overrides apply to the generic
`bash searchfu.sh` developer interface.

## Only keyword events, then failure

The local MiniLM model may not be cached. To provision it deliberately, run an
indexing build with `--allow-model-download`. Searches themselves remain offline.
Earlier keyword events are still useful. Background jobs emit a bounded failure
category and exception class; they do not dump private paths or prompts.

## Search is slow

A compressed-cache pass visits every stored cache vector. A full-precision pass
visits original vectors in SQLite. Large indexes therefore take time even when
warm. `stream` delivers snapshots; tool runners that buffer stdout should use
`start` and `poll`. Use short distinctive terms, several short query variants in
one job, `--fts` for lexical tasks, and cancel unnecessary deeper work.

Put the index on fast storage if possible; source files can remain on a slow
or disconnected drive. There are no assumed NVMe/USB speed multipliers. See
[performance](performance.md) for reproducible measurements and their limits.

## Matrix missing or incomplete

Search uses SQLite vectors with progressive `deep` events. To build the cache
from already indexed embeddings, set `SEARCHFU_DIR`/`SEARCHFU_ANN_DIR` and run
`python3 ann.py build`. To compact deleted rows or change precision, use
`python3 ann.py build --full --dtype int8`. Neither command reads source files.
A failed mutation invalidates cache metadata, so search cannot mistake partial
blocks for a complete generation. Resume the same dtype or rebuild from SQLite.

## Matrix identity or filter mismatch

Point both DB and matrix settings at the matching index. New caches record DB
identity and filter version. A mismatched cache fails rather than returning
plausible results from unrelated chunk IDs. Use `--full` to rebuild the cache
from the correct SQLite DB. Older caches without identity remain readable;
rebuilding adds the binding.

## Verification

`python3 ann.py verify --n 20` compares compressed results with original float32
vectors using sampled stored vectors as queries. It never loads a query model
or reads sources. Low recall means compression lost candidates. Full-precision
candidate refinement improves ordering but cannot guarantee recovering all
missing neighbors; `--deep` scores the original vectors exhaustively.

## Locks and interrupted builds

Index writers and matrix writers use advisory `flock` locks. The OS releases
these when the owning process exits; an existing lock file does not mean the
lock is held. Do not delete a lock file to bypass a live writer: that creates a
second lock inode and defeats coordination. Cancel waiting searches through
the normal job controls.

## Storage or memory pressure

SQLite includes original vectors, text and FTS; the compressed cache is extra.
Capacity admission reserves free space but cannot predict competing writers.
Large source files are read/chunked in memory during indexing. Search dequantizes
bounded slices rather than an entire million-row disk block. Long SQLite read
snapshots can retain WAL pages during concurrent indexing; finish or cancel
unneeded searches.

## Background jobs

`poll JOB_ID --after LAST_SEQUENCE` retrieves later events. Continue polling
until a terminal status and until all event pages have been consumed. Cancel
is cooperative: model operations and native math currently executing must
return before the next check. A dead worker is reported as failed; earlier
events remain. Job results persist in the owner-only state directory until the
user removes that job directory. They are unencrypted private data.

A refresh status of `gpu_unavailable` means shared admission was refused or
Archon lacks `/lease/shared/*` support. Retrieval still works. Install shared
lease support locally; Searchfu never substitutes an exclusive lease.
