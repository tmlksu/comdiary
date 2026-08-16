from __future__ import annotations

from comdiary.models import Signal
from comdiary.transcript import apply_mic_policy, parse_transcript, speaker_stats

from .conftest import SAMPLE_LUNCH, SAMPLE_SHARED_MIC


def test_parses_speaker_labels():
    parsed = parse_transcript(SAMPLE_LUNCH)
    speakers = {u.speaker for u in parsed.utterances}
    assert {"田中 太郎", "鈴木 花子", "佐藤 健"} <= speakers
    assert parsed.utterances[0].text.startswith("おはようございます")


def test_headings_are_not_speakers():
    parsed = parse_transcript(SAMPLE_LUNCH)
    assert all("昼会" not in u.speaker for u in parsed.utterances)


def test_bold_and_timestamp_layouts():
    text = "**田中**: あ\n00:03 鈴木: い\n[00:04:05] 佐藤: う\n"
    parsed = parse_transcript(text)
    assert [u.speaker for u in parsed.utterances] == ["田中", "鈴木", "佐藤"]


def test_metadata_lines_are_not_speakers():
    parsed = parse_transcript("日時: 2026-08-13\nURL: https://example.com/x\n田中: 本題です\n")
    assert [u.speaker for u in parsed.utterances] == ["田中"]


def test_balanced_meeting_is_trusted():
    stats = speaker_stats(SAMPLE_LUNCH)
    assert stats.mic_mode == "per_speaker"
    assert stats.attribution == "reliable"


def test_shared_mic_is_detected():
    stats = speaker_stats(SAMPLE_SHARED_MIC)
    assert stats.mic_mode == "shared"
    assert stats.attribution == "uncertain"
    assert "共通マイク" in stats.note


def test_no_labels_at_all():
    stats = speaker_stats("ただの平文のメモです。\n特に話者はいません。\n")
    assert stats.total_lines == 0
    assert stats.attribution == "uncertain"


def test_mic_policy_strips_attribution_when_uncertain():
    stats = speaker_stats(SAMPLE_SHARED_MIC)
    signals = [
        Signal(kind="escalation", topic="納期", speaker="会議室A", evidence="語気が強い", confidence=0.9)
    ]
    apply_mic_policy(signals, stats)
    assert signals[0].speaker is None
    assert signals[0].confidence <= 0.5
    assert "会議室A" in signals[0].evidence  # observation is kept, the claim is not


def test_mic_policy_keeps_attribution_when_reliable():
    stats = speaker_stats(SAMPLE_LUNCH)
    signals = [Signal(kind="concern", topic="納期", speaker="鈴木 花子", confidence=0.8)]
    apply_mic_policy(signals, stats)
    assert signals[0].speaker == "鈴木 花子"
    assert signals[0].confidence == 0.8
