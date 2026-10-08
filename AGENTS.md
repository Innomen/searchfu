# Developer agent boundary

Read PRIVACY.md before using Searchfu. Application source and generated
synthetic tests are authorized developer material. Runtime indexes, stored
text/vectors, user queries, job events, source paths and results are private.
Do not read, list, search or summarize them into remote tools. This includes
legacy index/, custom SEARCHFU_DB/SEARCHFU_ANN_DIR/SEARCHFU_JOBS_DIR locations,
~/.local/share/searchfu and ~/.local/state/searchfu. Do not enumerate these
paths. Ignore files do not grant permission or provide a security sandbox.

Use only `status --agent` for real-index diagnostics. Run tests and benchmarks
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
ignore inherited DB/ANN/candidate/data overrides. No backup discovery/fallback.
Generic developer interfaces retain explicit paths for synthetic tests and
deliberate migrations. Test deployed launcher and adapter parity after edits.

Default refresh policy (owner, 2026-10-07): bounded known-file repair overlaps
retrieval. Use the shared Archon lease, batches <=8 and 256 MiB allocator cap;
never unload the LLM or fall back to exclusive admission. --no-refresh opts out.
Bulk indexing retains its priority lease. Test cancellation through worker exit.
