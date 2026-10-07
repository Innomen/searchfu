# Privacy contract

Searchfu runs locally. Indexes, query requests, job events and search results
contain private data. They are not developer diagnostics and must never be
copied into a remote coding agent, tool response, tooltip, trace or bug report.
A tooltip can expose content just as a file read can.

Default indexes live in XDG_DATA_HOME/searchfu and job state in
XDG_STATE_HOME/searchfu/jobs, outside application source. Job directories are
mode 0700 and request/event/state files 0600. Requests are removed when a worker
finishes; results remain locally until their job directory is deliberately
removed. These files are plaintext, not encryption or protection from other
processes running as the same user. Background worker arguments contain a job
identifier, not the query. Foreground CLI queries can appear in shell history
and process arguments. Use the Python API when that distinction matters.

Do not publish legacy index directories, exported results, custom corpus paths
or job directories. Ignore rules reduce accidental commits but do not enforce
access control. Moving a legacy index is an explicit local operation; no
cleanup script walks, exports or relocates a user's corpus.

Remote developer agents may inspect source, developer documentation, generated
synthetic fixtures and `status --agent`. They must not run search, stats, poll,
verification or benchmarks against a real index, inspect runtime state, or
list personal source paths. Tests and benchmarks create their own synthetic
indexes; do not replace those fixtures with user data.

A trusted local worker can inspect separately authorized private material and
return only a narrow, agreed aggregate: counts, timings or boolean checks.
Validate its response locally against that aggregate schema before forwarding.
Unexpected responses stay local; summaries and filenames are not automatically
safe. Local workers are a privacy buffer, not a way to extract private content
through a different tool. Explicit indexing is the only corpus traversal;
search reads stored indexes by default. Opt-in --refresh inspects only a bounded
set of known files locally; it never discovers files by walking directories.
Refresh child requests travel over stdin, never process arguments or logs.
Remote developers still must not invoke it against real private content.

Collection configuration contains private roots and mount-device identities and
lives outside source. Filename-only catalogs are still private data. Content
hashes and stable result IDs are not anonymization. Imported RAG metadata and
passages remain local; aggregate migration checks must never dump them.
