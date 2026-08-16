"""Discovering and reading input documents.

Today there is one source: a directory that something else fills — commonly a
Google Drive stream mount on Windows (``G:\\...``) that an external script
writes finished transcripts into. Nothing below assumes POSIX, and nothing
assumes a file is ready the moment it appears — see ``quiet_seconds``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from ..util import sha256_file

TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".vtt", ".srt"}


@dataclass
class SourceDoc:
    path: Path
    name: str
    text: str
    sha256: str
    kind: str = "meeting"
    mtime: float = 0.0


def read_text(path: Path) -> str:
    """Read with a couple of realistic fallbacks (GAS/Windows exports)."""
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp932", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def load_doc(path: Path, kind: str = "meeting") -> SourceDoc:
    return SourceDoc(
        path=path,
        name=path.name,
        text=read_text(path),
        sha256=sha256_file(path),
        kind=kind,
        mtime=path.stat().st_mtime,
    )


def discover(
    inbox: Path,
    glob: str = "*.md",
    limit: int = 5,
    quiet_seconds: int = 60,
    now: float | None = None,
) -> tuple[list[Path], list[Path]]:
    """Return (ready, not-yet-settled).

    A file must have been untouched for ``quiet_seconds`` before we look at it.
    That covers both a writer still appending (truncation would be invisible
    afterwards) and a synced volume that has not finished materialising it.
    """
    if not inbox.is_dir():
        raise FileNotFoundError(f"取り込み元フォルダが見つかりません: {inbox}")
    now = now if now is not None else time.time()
    ready: list[Path] = []
    warming: list[Path] = []
    for path in sorted(inbox.glob(glob), key=lambda p: p.stat().st_mtime):
        if not path.is_file() or path.name.startswith((".", "~$")):
            continue
        if now - path.stat().st_mtime < quiet_seconds:
            warming.append(path)
            continue
        ready.append(path)
    return ready[:limit], warming


def iter_paths(target: Path, glob: str = "*") -> list[Path]:
    """Expand a user-supplied file or directory into readable text documents."""
    if target.is_file():
        return [target]
    if target.is_dir():
        return sorted(
            p for p in target.glob(glob) if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES
        )
    raise FileNotFoundError(f"見つかりません: {target}")
