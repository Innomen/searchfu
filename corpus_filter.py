"""Corpus junk filter.

Single authoritative definition of what counts as generated junk for the
vector path. Applied at three layers, all importing this module:

- index-build time: new junk files are not chunked or embedded (existing
  junk rows are kept, so nothing is ever deleted);
- ANN matrix build time: junk chunks are excluded from the vector matrix;
- query time: junk candidates are dropped from results.

Exclusion is filter-based and fully reversible. It is never blanket
size-based exclusion: real content includes legitimately huge files.
"""

FILTER_VERSION = 1

# Drive-relative root. Set via SEARCHFU_ROOT env var or leave empty for
# absolute-path passthrough.
import os
ROOT = os.environ.get("SEARCHFU_ROOT", "").rstrip("/")

# Path components (directories) that mark generated or install trees.
JUNK_COMPONENTS = {
    "node_modules", "dist", "build", ".cache", ".venv", "site-packages",
    "__pycache__", ".git", ".npm", ".cargo", ".rustup", ".pyenv",
    "appdata", "site-packages",
}

# File-name suffixes that mark generated artifacts.
JUNK_SUFFIXES = (".min.js", ".min.css", ".js.map", ".css.map", ".lock", ".sum")

# Lockfile names whose junk marker sits mid-name ("package-lock.json").
JUNK_NAMES = {"package-lock.json", "pnpm-lock.yaml"}


def normalize(path: str) -> str:
    """Root-relative form, regardless of how the row was stored.

    The files table may mix conventions: absolute paths from the original
    build and root-relative paths from incremental builds."""
    p = path
    if ROOT:
        prefix = ROOT + "/"
        if p.startswith(prefix):
            p = p[len(prefix):]
    return p


def is_junk(path: str) -> bool:
    parts = normalize(path).split("/")
    if any(c.lower() in JUNK_COMPONENTS for c in parts[:-1]):
        return True
    base = parts[-1].lower()
    if base in JUNK_NAMES:
        return True
    return any(base.endswith(s) for s in JUNK_SUFFIXES)
