# Rolling back the search-refresh beta

Use `--no-refresh` to immediately use index-only retrieval. Python callers
pass `refresh=False`. The earlier opt-in beta is tagged
`search-refresh-beta-20261007`; reverting the default-refresh commit restores it.
No beta schema migration is required. Each refreshed file is replaced in one
transaction, and the previous retrieval code can read the updated chunks and
vectors. Git stores source history, not the private database or its content.
A source rollback does not restore older indexed file contents.

The annotated `pre-refresh-beta-20261007` tag marks main before this release.
The `search-refresh-beta-20261007` tag identifies its release commit. To undo
that commit while preserving history and any subsequent changes:

```bash
git revert search-refresh-beta-20261007
```

Review and resolve conflicts if later commits touch the same files. After
verification, publish the revert with `git push origin main`. No force push or
reset of main is required.

Installed launchers and external adapters are deployment artifacts outside Git.
For the EMS installation, a compatible adapter and pinned launcher were saved
locally under `~/.local/share/searchfu-rollback/pre-refresh-beta-20261007/` before
publication. Restore the saved adapter to its developer location when reverting
Searchfu; it works without the beta's runtime_paths module and rejects requests
for the unavailable beta feature. The pinned launcher is self-contained and can
remain installed. Do not reinstall the old environment-configurable launcher if
the installation must continue ignoring backup index settings.

No runtime database, query, result or collection configuration is part of this
rollback bundle or either Git tag. Database content backups remain a separate,
explicit local operation.

For the default-refresh deployment, developer-only copies of the preceding EMS
adapter and Archon source are saved in
`~/.local/share/searchfu-rollback/default-refresh-20261007/`. Restore those only
if no subsequent source changes would be overwritten; preserve unrelated edits.
