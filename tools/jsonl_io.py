"""Append one JSON line to a file safely when several processes write at once.

The whole line is written with a single write() while holding an exclusive file lock, so two
supervisors (or a supervisor and the deploy tool) can never interleave half-lines.
POSIX uses fcntl.flock; Windows uses msvcrt.locking (the Windows branch is untested here).
"""
import json
import os
from pathlib import Path

try:
    import fcntl

    def _lock(f):
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)

    def _unlock(f):
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
except ImportError:  # Windows
    import msvcrt

    def _lock(f):
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock(f):
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def append_jsonl(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record) + "\n"
    with path.open("a", encoding="utf-8") as f:
        _lock(f)
        try:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        finally:
            _unlock(f)
