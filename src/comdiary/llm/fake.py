"""A deterministic offline backend.

Used by the test suite and by `--llm fake`, so the whole pipeline (matching,
rendering, indexing, git) can be exercised without a network call or an API
bill. It splits on markdown headings and summarises by excerpt instead of
actually understanding anything — enough to stand in for a real model's shape.
"""

from __future__ import annotations

import re
from typing import TypeVar

from pydantic import BaseModel

from ..config import LLMConfig
from ..models import DetailResponse, OpenQuestion, Segment, SplitResponse
from .backend import compose_prompt

T = TypeVar("T", bound=BaseModel)

_TRANSCRIPT_RE = re.compile(r"<transcript>\s*(.*?)\s*</transcript>", re.DOTALL)
_HEADING_RE = re.compile(r"^#{1,3}\s+(.+)$", re.MULTILINE)
_SCOPE_RE = re.compile(r"のうち、\*\*「(?P<title>[^」]+)」")


def _sections(body: str) -> list[tuple[str, str]]:
    """[(heading, section text)] — the whole document under a synthetic heading
    when it has none."""
    matches = list(_HEADING_RE.finditer(body))
    if not matches:
        first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "議事")
        return [(first[:40], body)]
    out = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        out.append((m.group(1).strip(), body[m.end() : end].strip()))
    return out


def _excerpt(text: str, limit: int = 200) -> str:
    flat = " ".join(line.strip() for line in text.splitlines() if line.strip())
    return flat[:limit]


class FakeBackend:
    name = "fake"

    def __init__(self, cfg: LLMConfig | None = None) -> None:
        self.cfg = cfg or LLMConfig(backend="fake")
        self.calls: list[tuple[str, str]] = []

    def preflight(self) -> tuple[bool, str]:
        return True, "オフラインの疑似バックエンド"

    def probe(self) -> tuple[bool, str]:
        return True, "オフラインの疑似バックエンド"

    def release_context(self) -> None:
        return None

    def close(self) -> None:
        return None

    def complete_json(self, prompt: str, schema: type[T], context: str | None = None) -> T:
        prompt = compose_prompt(prompt, context)
        self.calls.append((schema.__name__, prompt))
        if schema is SplitResponse:
            return self._split(prompt)  # type: ignore[return-value]
        if schema is DetailResponse:
            return self._detail(prompt)  # type: ignore[return-value]
        return schema.model_validate({})

    # ------------------------------------------------------------------
    def _split(self, prompt: str) -> SplitResponse:
        sections = _sections(self._transcript(prompt))
        segments = [
            Segment(
                segment_id=f"s{i}",
                title=heading,
                # A real model summarises the *content*; matching depends on that,
                # so the stand-in must put content here too, not just the heading.
                summary=_excerpt(text),
                span=heading,
            )
            for i, (heading, text) in enumerate(sections, start=1)
        ]
        return SplitResponse(
            title=sections[0][0],
            attendees=[],
            summary="(fake backend による仮サマリ)",
            segments=segments,
        )

    def _detail(self, prompt: str) -> DetailResponse:
        body = self._transcript(prompt)
        scope = _SCOPE_RE.search(prompt)
        if scope:
            wanted = scope.group("title")
            for heading, text in _sections(body):
                if heading == wanted:
                    body = text
                    break
        questions = [
            OpenQuestion(question=line.split(":", 1)[-1].strip())
            for line in body.splitlines()
            if line.strip().endswith(("？", "?"))
        ][:3]
        return DetailResponse(summary=_excerpt(body), open_questions=questions)

    @staticmethod
    def _transcript(prompt: str) -> str:
        m = _TRANSCRIPT_RE.search(prompt)
        return m.group(1) if m else prompt
