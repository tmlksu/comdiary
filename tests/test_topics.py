from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from comdiary.cli import app
from comdiary.config import Config
from comdiary.context import aggregate_topics, topic_detail
from comdiary.ingest.state import State
from comdiary.ledger.paths import LedgerPaths
from comdiary.models import Meeting, OpenQuestion, Segment, Signal, SpeakerStats
from comdiary.util import now

runner = CliRunner()


def _meeting(meeting_id: str, segments: list[Segment]) -> Meeting:
    return Meeting(
        meeting_id=meeting_id,
        title=f"会議 {meeting_id}",
        date=now(),
        segments=segments,
        speaker_stats=SpeakerStats(attribution="reliable", mic_mode="per_speaker"),
    )


def _seg(seg_id: str, project: str | None, topics: list[str], **kw) -> Segment:
    return Segment(
        segment_id=seg_id,
        title=f"話題 {seg_id}",
        summary="...",
        project_id=project,
        topics=topics,
        **kw,
    )


def _index(config: Config, *meetings: Meeting) -> None:
    with State(LedgerPaths(config.ledger).state_db) as state:
        for m in meetings:
            state.index_meeting(m, doc=f"/tmp/{m.meeting_id}.md", json_path=f"/tmp/{m.meeting_id}.json")


class TestRanking:
    def test_cross_project_reach_outranks_raw_frequency(self, config: Config):
        """A topic raised once in each of two projects is a stronger sign of a
        systemic gap than one raised repeatedly inside a single project."""
        _index(
            config,
            _meeting("m1", [_seg("s1", "alpha-migration", ["属人化"])]),
            _meeting("m2", [_seg("s1", "recruit-site", ["属人化"])]),
            _meeting("m3", [_seg("s1", "alpha-migration", ["切替日"]),
                            _seg("s2", "alpha-migration", ["切替日"])]),
            _meeting("m4", [_seg("s1", "alpha-migration", ["切替日"])]),
        )
        rows = aggregate_topics(config.ledger)
        assert [r["topic"] for r in rows][:2] == ["属人化", "切替日"]
        assert rows[0]["reach"] == 2 and rows[0]["count"] == 2
        assert rows[1]["reach"] == 1 and rows[1]["count"] == 3

    def test_unassigned_segments_count_as_reach(self, config: Config):
        """A topic nobody has filed anywhere is exactly the not-yet-a-project
        case this command exists to surface."""
        _index(
            config,
            _meeting("m1", [_seg("s1", "alpha-migration", ["引き継ぎ"])]),
            _meeting("m2", [_seg("s1", None, ["引き継ぎ"])]),
        )
        row = aggregate_topics(config.ledger)[0]
        assert row["unassigned"] == 1
        assert row["projects"] == ["alpha-migration"]
        assert row["reach"] == 2
        assert row["cross_project"]

    def test_single_project_topic_is_not_flagged(self, config: Config):
        _index(config, _meeting("m1", [_seg("s1", "alpha-migration", ["切替日"])]))
        assert aggregate_topics(config.ledger)[0]["cross_project"] is False

    def test_min_count_filters(self, config: Config):
        _index(
            config,
            _meeting("m1", [_seg("s1", "alpha-migration", ["一度きり"])]),
            _meeting("m2", [_seg("s1", "recruit-site", ["二度目"])]),
            _meeting("m3", [_seg("s1", "alpha-migration", ["二度目"])]),
        )
        rows = aggregate_topics(config.ledger, min_count=2)
        assert [r["topic"] for r in rows] == ["二度目"]

    def test_duplicate_topics_in_one_segment_count_once(self, config: Config):
        _index(config, _meeting("m1", [_seg("s1", "alpha-migration", ["納期", "納期"])]))
        assert aggregate_topics(config.ledger)[0]["count"] == 1


class TestEnrichment:
    def test_signals_and_questions_attach_to_the_topic(self, config: Config):
        seg = _seg(
            "s1",
            "alpha-migration",
            ["外注費"],
            signals=[
                Signal(kind="resistance", topic="外注費", speaker="tanaka",
                       intensity="high", concern="単価が上がり続けている"),
            ],
            open_questions=[OpenQuestion(question="上限を決めるか")],
        )
        _index(config, _meeting("m1", [seg]))
        row = aggregate_topics(config.ledger)[0]
        assert row["people"] == ["田中 太郎"]
        assert row["kinds"] == ["resistance"]
        assert row["max_intensity"] == "high"
        assert row["concerns"] == ["単価が上がり続けている"]
        assert row["open_questions"] == ["上限を決めるか"]

    def test_a_quiet_topic_still_appears(self, config: Config):
        """Signals are only recorded when something is notable. Ranking off them
        would hide precisely the subjects nobody is loud about."""
        _index(config, _meeting("m1", [_seg("s1", None, ["ドキュメント整備"])]))
        row = aggregate_topics(config.ledger)[0]
        assert row["topic"] == "ドキュメント整備"
        assert row["kinds"] == []

    def test_detail_lists_every_occurrence(self, config: Config):
        _index(
            config,
            _meeting("m1", [_seg("s1", "alpha-migration", ["属人化"])]),
            _meeting("m2", [_seg("s1", None, ["属人化"])]),
        )
        detail = topic_detail(config.ledger, "属人化")
        assert detail is not None
        assert len(detail["occurrences"]) == 2
        assert {o["project"] for o in detail["occurrences"]} == {"alpha-migration", None}

    def test_detail_returns_none_for_an_unknown_topic(self, config: Config):
        assert topic_detail(config.ledger, "存在しない") is None


class TestVocabularyConvergence:
    def test_known_topics_are_offered_back_to_the_extractor(self, config: Config):
        """Labels must converge or the aggregate fragments into synonyms."""
        _index(
            config,
            _meeting("m1", [_seg("s1", "alpha-migration", ["納期"])]),
            _meeting("m2", [_seg("s1", "recruit-site", ["納期"]), _seg("s2", None, ["外注費"])]),
        )
        with State(LedgerPaths(config.ledger).state_db) as state:
            known = state.known_topics()
        assert known[0] == "納期"
        assert "外注費" in known

    def test_known_topics_reach_the_detail_prompt(self, ledger: Path):
        from comdiary.llm.prompts import detail_prompt
        from comdiary.registry.store import Registry

        prompt = detail_prompt(
            "本文", "話題", "", None, Registry.load(ledger), SpeakerStats(),
            known_topics=["納期", "外注費"],
        )
        assert "既出の論点" in prompt
        assert "納期、外注費" in prompt


class TestCli:
    def _config_file(self, tmp_path: Path, config: Config) -> Path:
        path = tmp_path / "comdiary.toml"
        path.write_text(f"ledger = '{config.ledger.as_posix()}'\n[git]\nenabled = false\n",
                        encoding="utf-8")
        return path

    def test_topics_json(self, config: Config, tmp_path: Path):
        _index(
            config,
            _meeting("m1", [_seg("s1", "alpha-migration", ["属人化"])]),
            _meeting("m2", [_seg("s1", None, ["属人化"])]),
        )
        result = runner.invoke(
            app, ["topics", "--config", str(self._config_file(tmp_path, config)), "--json"]
        )
        assert result.exit_code == 0, result.output
        rows = json.loads(result.stdout)
        assert rows[0]["topic"] == "属人化"
        assert rows[0]["cross_project"] is True

    def test_topics_show(self, config: Config, tmp_path: Path):
        _index(config, _meeting("m1", [_seg("s1", "alpha-migration", ["属人化"])]))
        result = runner.invoke(
            app,
            ["topics", "--show", "属人化", "--config", str(self._config_file(tmp_path, config))],
        )
        assert result.exit_code == 0, result.output
        assert "属人化" in result.output

    def test_topics_show_unknown_fails(self, config: Config, tmp_path: Path):
        result = runner.invoke(
            app,
            ["topics", "--show", "無い", "--config", str(self._config_file(tmp_path, config))],
        )
        assert result.exit_code == 1

    def test_empty_ledger_explains_itself(self, config: Config, tmp_path: Path):
        result = runner.invoke(
            app, ["topics", "--config", str(self._config_file(tmp_path, config))]
        )
        assert result.exit_code == 0, result.output
        assert "論点がありません" in result.output
