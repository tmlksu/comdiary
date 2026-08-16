from __future__ import annotations

from pathlib import Path

from comdiary.models import ProjectGuess, Segment
from comdiary.registry.matcher import match_segment
from comdiary.registry.store import Registry


def seg(title: str, summary: str = "", guess: ProjectGuess | None = None) -> Segment:
    return Segment(
        segment_id="s1", title=title, summary=summary, guess=guess or ProjectGuess()
    )


def test_alias_match_wins(ledger: Path):
    registry = Registry.load(ledger)
    result = match_segment(seg("基盤移行の切替日"), registry)
    assert result.project_id == "alpha-migration"
    assert result.method == "alias"


def test_keyword_match_needs_two_hits(ledger: Path):
    registry = Registry.load(ledger)
    one = match_segment(seg("進捗共有", "デザイン案を見た"), registry)
    assert one.project_id is None

    two = match_segment(seg("進捗共有", "デザイン案と外注費の話"), registry)
    assert two.project_id == "recruit-site"
    assert two.method == "keyword"


def test_llm_guess_accepted_above_threshold(ledger: Path):
    registry = Registry.load(ledger)
    result = match_segment(
        seg("来期の話", guess=ProjectGuess(project_id="alpha-migration", confidence=0.9)),
        registry,
        min_confidence=0.6,
    )
    assert result.project_id == "alpha-migration"
    assert result.method == "llm"


def test_low_confidence_guess_goes_to_inbox(ledger: Path):
    registry = Registry.load(ledger)
    result = match_segment(
        seg("来期の話", guess=ProjectGuess(project_id="alpha-migration", confidence=0.3)),
        registry,
        min_confidence=0.6,
    )
    assert result.project_id is None
    assert result.method == "unmatched"


def test_hallucinated_project_id_is_rejected(ledger: Path):
    registry = Registry.load(ledger)
    result = match_segment(
        seg("謎の議題", guess=ProjectGuess(project_id="does-not-exist", confidence=0.99)),
        registry,
    )
    assert result.project_id is None


def test_ambiguous_multi_alias_needs_human(ledger: Path):
    registry = Registry.load(ledger)
    result = match_segment(seg("基盤移行と採用サイトの合同レビュー"), registry)
    assert result.project_id is None
    assert "複数" in result.rationale


def test_llm_guess_breaks_multi_alias_tie(ledger: Path):
    registry = Registry.load(ledger)
    result = match_segment(
        seg(
            "基盤移行と採用サイトの合同レビュー",
            guess=ProjectGuess(project_id="recruit-site", confidence=0.8),
        ),
        registry,
    )
    assert result.project_id == "recruit-site"


def test_suggested_new_project_is_surfaced(ledger: Path):
    registry = Registry.load(ledger)
    result = match_segment(
        seg("新規の話", guess=ProjectGuess(suggested_name="オフィス移転")), registry
    )
    assert result.project_id is None
    assert "オフィス移転" in result.rationale
