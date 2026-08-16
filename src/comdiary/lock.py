"""A portable single-writer lock.

`fcntl.flock` is not available on Windows, and the ingest host is expected to be
a Windows box (the Google Drive stream mounts at ``G:\\``), so this uses an
exclusive-create lockfile with a staleness timeout instead. Good enough for
"do not let two cron ticks overlap" on a single machine.
"""

from __future__ import annotations

import json
import os
import socket
import time
from contextlib import contextmanager
from pathlib import Path

from .util import ensure_dir

DEFAULT_STALE_SECONDS = 3600


class LockBusy(RuntimeError):
    pass


def _payload() -> str:
    return json.dumps(
        {"pid": os.getpid(), "host": socket.gethostname(), "at": time.time()}, ensure_ascii=False
    )


def _is_stale(path: Path, stale_seconds: int) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        age = time.time() - float(data.get("at", 0))
    except (OSError, ValueError):
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return True
    return age > stale_seconds


@contextmanager
def file_lock(path: Path, stale_seconds: int = DEFAULT_STALE_SECONDS):
    ensure_dir(path.parent)
    acquired = False
    try:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if not _is_stale(path, stale_seconds):
                raise LockBusy(
                    f"別の comdiary が実行中です (ロック: {path})。"
                    " 停止済みなら手動で削除してください。"
                ) from None
            path.unlink(missing_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        acquired = True
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_payload())
        yield path
    finally:
        if acquired:
            path.unlink(missing_ok=True)
