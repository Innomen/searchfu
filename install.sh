#!/usr/bin/env bash
# Install this already-cloned revision; runtime data remains outside the app.
set -euo pipefail
APP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_DIR="${SEARCHFU_ENV:-${XDG_DATA_HOME:-$HOME/.local/share}/searchfu-env}"
BIN_DIR="${SEARCHFU_BIN:-$HOME/.local/bin}"
"${SEARCHFU_BOOTSTRAP_PY:-python3}" -m venv --system-site-packages "$ENV_DIR"
"$ENV_DIR/bin/python" -m pip install -r "$APP/requirements.txt"
mkdir -p "$BIN_DIR"
"$ENV_DIR/bin/python" - "$APP" "$ENV_DIR" "$BIN_DIR" <<'PY'
import pathlib,sys
app,env,bin_dir=map(pathlib.Path,sys.argv[1:])
sys.path.insert(0,str(app))
from runtime_paths import launcher_text
launcher=bin_dir/'searchfu'
launcher.write_text(launcher_text(app,env))
launcher.chmod(0o755)
PY
"$ENV_DIR/bin/python" "$APP/search.py" stream --help >/dev/null
printf 'Searchfu installed; runtime indexes remain outside application source.\n'
