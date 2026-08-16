from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from pathlib import Path

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SPACES = re.compile(r"\s+")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def short_hash(value: str, n: int = 8) -> str:
    return sha256_text(value)[:n]


def safe_filename(name: str, max_len: int = 60) -> str:
    """Filesystem-safe, but keeps Japanese — readability beats ASCII purity here."""
    name = unicodedata.normalize("NFC", name).strip()
    name = _UNSAFE.sub("", name)
    name = _SPACES.sub("-", name)
    name = name.strip("-. ")
    if len(name) > max_len:
        name = name[:max_len].rstrip("-. ")
    return name or "untitled"


def slugify_ascii(name: str, fallback: str = "item") -> str:
    """ASCII slug for registry ids. Returns '' when the input has no ASCII to work with."""
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", "ignore").decode("ascii").lower()
    name = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    return name or fallback


def normalize_match(text: str) -> str:
    """Normalization used for alias/keyword matching (width, case, spacing)."""
    text = unicodedata.normalize("NFKC", text).lower()
    return _SPACES.sub("", text)


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def now() -> datetime:
    return datetime.now().astimezone()


def iso(dt: datetime | None) -> str:
    return dt.isoformat(timespec="seconds") if dt else ""


def atomic_write(path: Path, content: str) -> None:
    ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
