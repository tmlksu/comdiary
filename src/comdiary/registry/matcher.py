"""Assign each segment to a project.

Two-stage on purpose:

1. Deterministic alias / keyword matching. Cheap, reproducible, auditable.
2. The LLM's own guess, accepted only above a confidence floor.

Anything else goes to ``_inbox`` for `comdiary triage`. We never create a
project unattended: a wrong auto-created project pollutes the registry in a way
that is tedious to unwind, while an un-triaged segment costs one command.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Project, Segment
from ..util import normalize_match
from .store import Registry

#: Aliases shorter than this match far too eagerly inside Japanese prose.
MIN_ALIAS_LEN = 2
#: One keyword hit is noise; this many distinct hits is a signal.
KEYWORD_HITS_REQUIRED = 2


@dataclass
class MatchResult:
    project_id: str | None
    method: str
    confidence: float
    rationale: str


def _haystack(segment: Segment) -> str:
    parts = [segment.title, segment.summary, segment.span]
    parts += [d.what for d in segment.decisions]
    parts += [a.what for a in segment.actions]
    parts += [q.question for q in segment.open_questions]
    parts += segment.next_agenda
    return normalize_match(" ".join(p for p in parts if p))


def _alias_hit(project: Project, hay: str) -> str | None:
    for alias in [project.name, *project.aliases]:
        norm = normalize_match(alias)
        if len(norm) >= MIN_ALIAS_LEN and norm in hay:
            return alias
    return None


def _keyword_hits(project: Project, hay: str) -> list[str]:
    hits = []
    for kw in project.keywords:
        norm = normalize_match(kw)
        if len(norm) >= MIN_ALIAS_LEN and norm in hay:
            hits.append(kw)
    return hits


def match_segment(
    segment: Segment,
    registry: Registry,
    min_confidence: float = 0.6,
) -> MatchResult:
    if segment.project_id and segment.match_method == "manual":
        return MatchResult(segment.project_id, "manual", 1.0, "手動指定")

    hay = _haystack(segment)
    candidates = [p for p in registry.projects if p.status != "closed"] or registry.projects

    alias_hits = [(p, a) for p in candidates if (a := _alias_hit(p, hay))]
    if len(alias_hits) == 1:
        project, alias = alias_hits[0]
        return MatchResult(project.id, "alias", 0.95, f"別名 '{alias}' に一致")

    keyword_hits = [
        (p, hits)
        for p in candidates
        if len(hits := _keyword_hits(p, hay)) >= KEYWORD_HITS_REQUIRED
    ]

    # Several aliases matched — let the LLM's guess break the tie if it picked
    # one of them, otherwise this is genuinely ambiguous and needs a human.
    if len(alias_hits) > 1:
        guessed = segment.guess.project_id
        names = ", ".join(p.id for p, _ in alias_hits)
        if guessed in {p.id for p, _ in alias_hits}:
            return MatchResult(guessed, "alias", 0.8, f"複数候補({names})をLLM推定で決定")
        return MatchResult(None, "unmatched", 0.0, f"複数の案件に一致し確定できず: {names}")

    if len(keyword_hits) == 1:
        project, hits = keyword_hits[0]
        return MatchResult(project.id, "keyword", 0.8, f"キーワード一致: {', '.join(hits)}")

    guess = segment.guess
    if guess.project_id and registry.project(guess.project_id):
        if guess.confidence >= min_confidence:
            return MatchResult(guess.project_id, "llm", guess.confidence, guess.rationale or "LLM推定")
        return MatchResult(
            None, "unmatched", guess.confidence,
            f"LLM推定 '{guess.project_id}' は確信度 {guess.confidence:.2f} で閾値未満",
        )

    if guess.suggested_name:
        return MatchResult(None, "unmatched", 0.0, f"新規案件の可能性: {guess.suggested_name}")
    return MatchResult(None, "unmatched", 0.0, "該当する案件を特定できませんでした")


def match_all(segments: list[Segment], registry: Registry, min_confidence: float = 0.6) -> None:
    """Fill in ``project_id`` / ``match_method`` in place."""
    for segment in segments:
        result = match_segment(segment, registry, min_confidence)
        segment.project_id = result.project_id
        segment.match_method = result.method  # type: ignore[assignment]
        if result.rationale and not segment.guess.rationale:
            segment.guess.rationale = result.rationale
