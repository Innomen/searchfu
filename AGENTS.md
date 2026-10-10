# Developer agent boundary

Read PRIVACY.md before using Searchfu. Application source and generated
synthetic tests are authorized developer material. Runtime indexes, stored
text/vectors, user queries, job events, source paths and results are private.
Do not read, list, search or summarize them into remote tools. This includes
legacy index/, custom SEARCHFU_DB/SEARCHFU_ANN_DIR/SEARCHFU_JOBS_DIR locations,
~/.local/share/searchfu and ~/.local/state/searchfu. Do not enumerate these
paths. Ignore files do not grant permission or provide a security sandbox.

Use only `status --agent`, the closed `metrics --agent` surface (including
`--footprint`), `errors --agent`, and `gui-control status` for real-index
diagnostics. The owner-authorized GUI controller can reload the app and submit
an explicitly authorized query; consume only its sanitized replies, never live
result widgets. Search text travels locally via stdin, not argv. Run tests and benchmarks
only with their generated fixtures. Inspect code to diagnose runtime errors;
request narrowly sanitized local checks when private access is needed.
A local worker may have broader explicitly authorized access, but its response
must pass an agreed aggregate-only schema locally before remote forwarding.

Search must never crawl source files. Corpus traversal belongs only to explicit
build/update indexing. The owner-authorized default refresh may inspect a bounded
set of already cataloged plain-text files and bank changes without traversal. Preserve early results and cancel/poll contracts across
CLI, Python, background workers and documentation. Test the public shell route.

The owner authorized repository publication, installation and local deployment
on 2026-10-06. Real-corpus evaluation and indexing stay local and return only
validated counts, timings and boolean checks to remote advisers. Collection
configuration is private. Do not enumerate roots or runtime files. See
docs/competitive-direction.md and docs/collections.md for the current design.

Publication policy (owner, 2026-10-06): the repository may be public, but
remains all rights reserved. Do not substitute an open-source license.
The owner objects to granting military use permission; any future proposal
allowing reuse must address that restriction and requires the owner's choice.
Public visibility and permission to reuse are separate decisions.

Searchfu-first integration policy: local consumers should use indexed Searchfu
for filename, keyword and semantic discovery, with short queries, explicit
collections and progressive cancellation. Privacy rules above still apply to
remote developers. Exact verification of known source with `rg` is a specialized
exception, not permission to crawl a private corpus. Record concrete limitations
and fallback needs in docs/feature-requests.md; do not silently replace Searchfu
with another default search tool. Keep all examples and validation synthetic.

Installed index location policy (owner, 2026-10-07): runtime_paths.py is the
authoritative installed path family. The installed launcher and EMS adapter
ignore inherited DB/ANN/candidate/data overrides. No backup discovery/fallback. The owner-authorized USB comparison target is
a separate explicit, read-only shadow; it must never replace primary results.
Generic developer interfaces retain explicit paths for synthetic tests and
deliberate migrations. Test deployed launcher and adapter parity after edits.

Explicitly owner-authorized metadata repair may use
`metadata-upgrade --comparison-target`; consume only its closed aggregate
reports. It operates locally on known catalog paths and never exports their
names, contents or cache identities. It is not an automatic search migration.

Default refresh policy (owner, 2026-10-07): bounded known-file repair overlaps
retrieval. Use the shared Archon lease, batches <=8 and 256 MiB allocator cap;
never unload the LLM or fall back to exclusive admission. --no-refresh opts out.
Bulk indexing retains its priority lease. Test cancellation through worker exit.

Continuous experiment policy (owner, 2026-10-10): preserve partial search evidence locally and bank closed numeric progress independently of completed pairs. A configured time limit of 0 means unlimited across GUI and USB; cancellation still owns child termination. Private evidence journals are never an agent-readable surface. Do not treat a deadline or absent final pair as permission to discard observations.

Replica contract (owner, 2026-10-10): one logical computer-wide index, with
NVMe authoritative and USB a matching replica, not independently growing
corpora. An explicit `replicate --comparison-target --wait` copies a stable
SQLite snapshot and matching caches without crawling or embedding, retaining
the preceding USB artifacts. Do not replace artifacts beneath an active
experiment. `inventory-compare --comparison-target` is a closed aggregate-only
diagnostic: paths stay inside SQLite. File inventory overlap does not establish
text/vector equality. Timing pairs from different or unverified snapshots must
never be presented as a controlled hardware comparison. Replica freshness is
conservative; writes after replication invalidate the matched-snapshot status.
