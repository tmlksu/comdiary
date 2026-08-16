"""Idempotent markdown blocks.

Living docs are appended to forever, by both this tool and by humans. Every
machine-written chunk is fenced with HTML comments carrying a stable key, so a
re-run **replaces** its own previous output instead of duplicating it, and
never touches anything a human typed outside the fences.

    <!-- comdiary:begin key=meeting/abc123/s1 -->
    ...generated...
    <!-- comdiary:end key=meeting/abc123/s1 -->
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..util import atomic_write, ensure_dir

BEGIN = "<!-- comdiary:begin key={key} updated={updated} -->"
END = "<!-- comdiary:end key={key} -->"

_BLOCK_RE_TMPL = (
    r"[ \t]*<!--\s*comdiary:begin\s+key={key}(?:\s+updated=\S*)?\s*-->"
    r".*?"
    r"<!--\s*comdiary:end\s+key={key}\s*-->[ \t]*\n?"
)
_ANY_BLOCK_RE = re.compile(
    r"[ \t]*<!--\s*comdiary:begin\s+key=(?P<key>\S+)(?:\s+updated=(?P<updated>\S*))?\s*-->"
    r"(?P<body>.*?)"
    r"<!--\s*comdiary:end\s+key=(?P=key)\s*-->",
    re.DOTALL,
)


@dataclass
class Block:
    key: str
    body: str
    updated: str = ""


def wrap(key: str, body: str, updated: datetime | None = None) -> str:
    stamp = (updated or datetime.now().astimezone()).isoformat(timespec="seconds")
    body = body.strip("\n")
    return f"{BEGIN.format(key=key, updated=stamp)}\n{body}\n{END.format(key=key)}\n"


def find_blocks(text: str) -> list[Block]:
    return [
        Block(key=m.group("key"), body=m.group("body").strip("\n"), updated=m.group("updated") or "")
        for m in _ANY_BLOCK_RE.finditer(text)
    ]


def has_block(text: str, key: str) -> bool:
    return re.search(_BLOCK_RE_TMPL.format(key=re.escape(key)), text, re.DOTALL) is not None


def upsert(text: str, key: str, body: str, updated: datetime | None = None) -> tuple[str, bool]:
    """Replace the block with ``key`` or append it. Returns (text, changed)."""
    block = wrap(key, body, updated)
    pattern = re.compile(_BLOCK_RE_TMPL.format(key=re.escape(key)), re.DOTALL)
    existing = pattern.search(text)
    if existing:
        # Compare bodies only — an identical body must not churn the timestamp,
        # otherwise every re-run produces a meaningless git diff.
        current = find_blocks(existing.group(0))
        if current and current[0].body == body.strip("\n"):
            return text, False
        return pattern.sub(lambda _: block, text, count=1), True
    prefix = text if text.endswith("\n") or not text else text + "\n"
    if prefix and not prefix.endswith("\n\n"):
        prefix += "\n"
    return prefix + block, True


def upsert_file(
    path: Path,
    key: str,
    body: str,
    header: str = "",
    updated: datetime | None = None,
) -> bool:
    """Upsert a block into a file, creating it with ``header`` if missing."""
    ensure_dir(path.parent)
    text = path.read_text(encoding="utf-8") if path.is_file() else header
    new_text, changed = upsert(text, key, body, updated)
    if changed or not path.is_file():
        atomic_write(path, new_text)
    return changed


def strip_blocks(text: str) -> str:
    """Human-authored remainder — used when we need to know what a person wrote."""
    return _ANY_BLOCK_RE.sub("", text).strip()
