"""Capacity admission shared by DB and ANN writers; no indexed data access."""
import json, os, shutil
from pathlib import Path

class CapacityError(RuntimeError):
    pass

def require_capacity(path, estimated_bytes=0, phase="write"):
    target = Path(path)
    while not target.exists():
        target = target.parent
    free = shutil.disk_usage(target).free
    reserve = int(os.environ.get("SEARCHFU_RESERVE_BYTES", str(8 * 1024**3)))
    if reserve < 1024**3:
        raise ValueError("storage reserve must be at least one GiB")
    required = reserve + max(0, int(estimated_bytes))
    if free < required:
        print(json.dumps({"ok": False, "error": "insufficient_capacity",
                          "phase": phase, "free_bytes": free,
                          "required_bytes": required}), flush=True)
        raise CapacityError("insufficient_capacity")
    return free

def batch_bytes(texts):
    # Conservative allowance for float32 vectors, SQLite pages, FTS and WAL.
    # An admission estimate, not a guarantee against concurrent disk consumers.
    return sum((len(t.encode("utf-8")) * 4 + 4096) * 4 for t in texts)
