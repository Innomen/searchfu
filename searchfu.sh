#!/usr/bin/env bash
# searchfu — text-only hybrid search (MiniLM + FTS5) over a corpus root.
#
# CPU-only by default. Override with SEARCHFU_CUDA=1 to enable GPU encoding.
#
# Configuration (env vars):
#   SEARCHFU_ROOT     corpus root (default: current directory)
#   SEARCHFU_DIR      index directory (default: $XDG_DATA_HOME/searchfu or ~/.local/share/searchfu)
#   SEARCHFU_DB       SQLite DB path (default: $SEARCHFU_DIR/drive.sqlite3)
#   SEARCHFU_ANN_DIR  ANN matrix directory (default: $SEARCHFU_DIR/ann)
#   SEARCHFU_PY       Python interpreter (default: python3)
#   SEARCHFU_CUDA     set to 1 to enable GPU encoding
#
# Usage:
#   bash searchfu.sh build [roots...]      # default root: SEARCHFU_ROOT
#   bash searchfu.sh search "query" [--top-k 12] [--path PREFIX/] [--after YYYY-MM-DD]
#   bash searchfu.sh update [roots...]     # alias: crawl once, bank DB + matrix
#   bash searchfu.sh stream "tags" [--deep]  # flushed NDJSON events
#   bash searchfu.sh start "tags"            # background job id
#   bash searchfu.sh poll JOB [--after N]    # incremental events
#   bash searchfu.sh cancel JOB              # cooperative cancellation
#   bash searchfu.sh status --agent        # no private paths or text
#   bash searchfu.sh status
#   bash searchfu.sh stats                 # corpus census
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${SEARCHFU_ROOT:-.}"
PY="${SEARCHFU_PY:-python3}"
export SEARCHFU_DIR="${SEARCHFU_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/searchfu}"
DB="${SEARCHFU_DB:-$SEARCHFU_DIR/drive.sqlite3}"
export SEARCHFU_ANN_DIR="${SEARCHFU_ANN_DIR:-$SEARCHFU_DIR/ann}"
export CUDA_VISIBLE_DEVICES="${SEARCHFU_CUDA:+0}"
[ "${SEARCHFU_CUDA:-0}" = "1" ] || export CUDA_VISIBLE_DEVICES=""

cmd="${1:-status}"
if [ $# -gt 0 ]; then shift; fi
case "$cmd" in
  scope)
    exec "$PY" "$SCRIPT_DIR/catalog.py" --db "$DB" "$@"
    ;;
  build|update)
    mkdir -p "$(dirname "$DB")"
    exec 9>"$DB.update.lock"
    flock 9
    roots=("$@"); [ ${#roots[@]} -eq 0 ] && roots=("$ROOT")
    "$PY" "$SCRIPT_DIR/search.py" --db "$DB" build "${roots[@]}" --no-images
    SEARCHFU_ANN_DB="$DB" "$PY" "$SCRIPT_DIR/ann.py" build
    ;;
  candidates-build)
    exec "$PY" "$SCRIPT_DIR/candidates.py" --db "$DB" --dir "${SEARCHFU_CANDIDATES_DIR:-$(dirname "$DB")/candidates}" "$@"
    ;;
  search)
    exec "$PY" "$SCRIPT_DIR/search.py" --db "$DB" search --no-images "$@"
    ;;
  stream|start|poll|cancel)
    exec "$PY" "$SCRIPT_DIR/search.py" --db "$DB" "$cmd" "$@"
    ;;
  status)
    exec "$PY" "$SCRIPT_DIR/search.py" --db "$DB" status "$@"
    ;;
  stats)
    exec "$PY" "$SCRIPT_DIR/search.py" --db "$DB" stats
    ;;
  *)
    echo "usage: searchfu.sh {build|update [roots...] | search QUERY [opts] | stream QUERY | start QUERY | poll JOB | cancel JOB | status | stats}" >&2
    exit 2
    ;;
esac
