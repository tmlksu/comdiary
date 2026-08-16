"""Speaker-label parsing and microphone-mode heuristics.

Deliberately tolerant: the exact shape of the Google Meet / GAS export is not
pinned down yet (see docs/open-questions.md Q1), so we accept several common
layouts and always report how confident the parse was.

Nothing here calls an LLM. Speaker statistics must be deterministic, because
they are what we use to *distrust* the LLM's speaker attribution.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import SpeakerStats

#: A single shared conference mic makes one participant look like they said
#: everything. Above this share we stop trusting per-speaker attribution.
SHARED_MIC_THRESHOLD = 0.70
#: Below this many labelled lines the distribution is not meaningful either.
MIN_LINES_FOR_STATS = 6

_TIMESTAMP = r"(?:\[?\(?\d{1,2}:\d{2}(?::\d{2})?\)?\]?\s*[-–—]?\s*)"

_PATTERNS: tuple[re.Pattern[str], ...] = (
    # **田中 太郎:** こんにちは   /   **田中太郎**: こんにちは
    re.compile(rf"^\s*(?:[-*]\s*)?{_TIMESTAMP}?\*\*(?P<sp>[^*]{{1,40}}?)\*\*\s*[:：]?\s*(?P<txt>.*)$"),
    # 00:03:12 田中 太郎: こんにちは   /   田中 太郎: こんにちは
    re.compile(
        rf"^\s*(?:[-*]\s*)?{_TIMESTAMP}?(?P<sp>[^\s:：#>\[\]][^:：]{{0,39}}?)\s*[:：]\s+(?P<txt>.+)$"
    ),
)

# Headings, quotes, code fences, horizontal rules, table rows — never utterances.
# Note the rule pattern must not swallow "**田中**: ..." bold speaker labels.
_SKIP = re.compile(r"^\s*(?:#{1,6}\s|>|```|[-*_=]{3,}\s*$|\||$)")
#: Lines like "https://meet.google.com/xyz" or "Date: 2026-08-13" look like
#: "speaker: text" but are metadata. Reject speakers that are obviously not names.
_NOT_A_NAME = re.compile(
    r"^(https?|www|http|date|time|url|link|meeting|attendees?|参加者|日時|場所|議題|概要|要約|note|memo)$",
    re.IGNORECASE,
)


@dataclass
class Utterance:
    speaker: str
    text: str
    line_no: int


@dataclass
class ParsedTranscript:
    utterances: list[Utterance] = field(default_factory=list)
    unlabelled_lines: int = 0
    total_content_lines: int = 0

    @property
    def label_ratio(self) -> float:
        if self.total_content_lines == 0:
            return 0.0
        return len(self.utterances) / self.total_content_lines


def _match_speaker_line(line: str) -> tuple[str, str] | None:
    for pattern in _PATTERNS:
        m = pattern.match(line)
        if not m:
            continue
        speaker = m.group("sp").strip(" 　*_-")
        body = m.group("txt").strip()
        if not speaker or _NOT_A_NAME.match(speaker):
            return None
        # A "speaker" containing sentence punctuation is almost certainly prose.
        if any(ch in speaker for ch in "。、！？.!?"):
            return None
        return speaker, body
    return None


def parse_transcript(text: str) -> ParsedTranscript:
    parsed = ParsedTranscript()
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if not line.strip() or _SKIP.match(line):
            continue
        parsed.total_content_lines += 1
        hit = _match_speaker_line(line)
        if hit is None:
            parsed.unlabelled_lines += 1
        else:
            parsed.utterances.append(Utterance(speaker=hit[0], text=hit[1], line_no=i))
    return parsed


def speaker_stats(text: str) -> SpeakerStats:
    """Compute the distribution and decide whether we can trust speaker labels."""
    parsed = parse_transcript(text)
    counts: dict[str, int] = {}
    for utt in parsed.utterances:
        counts[utt.speaker] = counts.get(utt.speaker, 0) + 1

    total = sum(counts.values())
    stats = SpeakerStats(line_counts=dict(sorted(counts.items(), key=lambda kv: -kv[1])))
    stats.total_lines = total

    if total == 0:
        stats.mic_mode = "unknown"
        stats.attribution = "uncertain"
        stats.note = "話者ラベルを検出できませんでした。話者推定は行いません。"
        return stats

    stats.distribution = {k: round(v / total, 3) for k, v in stats.line_counts.items()}
    top_speaker, top_share = next(iter(stats.distribution.items()))

    if total < MIN_LINES_FOR_STATS:
        stats.mic_mode = "unknown"
        stats.attribution = "uncertain"
        stats.note = f"ラベル付き発言が{total}行のみで、分布判定に足りません。"
    elif len(counts) == 1:
        stats.mic_mode = "shared"
        stats.attribution = "uncertain"
        stats.note = (
            f"話者が{top_speaker}の1名のみ。共通マイクの可能性が高く、"
            "個別の話者帰属は行いません。"
        )
    elif top_share >= SHARED_MIC_THRESHOLD:
        stats.mic_mode = "shared"
        stats.attribution = "uncertain"
        stats.note = (
            f"{top_speaker}が全発言の{top_share:.0%}を占めており、共通マイクの疑いがあります。"
            "話者帰属は参考値として扱ってください。"
        )
    else:
        stats.mic_mode = "per_speaker"
        stats.attribution = "reliable"

    if parsed.label_ratio < 0.5 and stats.attribution == "reliable":
        stats.attribution = "uncertain"
        stats.note = (
            f"ラベル付き行が全体の{parsed.label_ratio:.0%}しかなく、"
            "取りこぼしがある可能性があります。"
        )
    return stats


def apply_mic_policy(signals: list, stats: SpeakerStats) -> list:
    """Strip / discount speaker attribution when the mic layout makes it unreliable.

    We keep the observation ("納期について強い押し返しがあった") and drop the
    claim about *who* said it, because a shared mic makes that claim wrong more
    often than right — and a wrong attribution about someone's temperament is
    worse than no attribution.
    """
    if stats.attribution == "reliable":
        return signals
    for sig in signals:
        if sig.speaker:
            note = f"(話者推定は不確実: {sig.speaker})"
            sig.evidence = f"{sig.evidence} {note}".strip()
            sig.speaker = None
        sig.confidence = round(min(sig.confidence, 0.5), 2)
    return signals
