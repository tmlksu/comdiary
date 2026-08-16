from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from comdiary.cli import app
from comdiary.config import Config
from comdiary.context import aggregate_concerns, build_pack
from comdiary.ingest.pipeline import Pipeline
from comdiary.ingest.state import State
from comdiary.ledger.paths import LedgerPaths
from comdiary.llm.fake import FakeBackend
from comdiary.models import Meeting, Signal, SpeakerStats

from .conftest import SAMPLE_LUNCH

runner = CliRunner()


def _config_file(tmp_path: Path, config: Config) -> Path:
    path = tmp_path / "comdiary.toml"
    path.write_text(
        f'ledger = "{config.ledger.as_posix()}"\n'
        "[ingest]\n"
        f'inbox = "{config.ingest.inbox.as_posix()}"\n'
        f'done = "{config.ingest.done.as_posix()}"\n'
        f'failed = "{config.ingest.failed.as_posix()}"\n'
        "quiet_seconds = 0\n"
        "[llm]\n"
        'backend = "fake"\n'
        "[git]\n"
        "enabled = false\n",
        encoding="utf-8",
    )
    return path


def _seed(config: Config) -> None:
    (config.ingest.inbox / "2026-08-13-1200-昼会.md").write_text(SAMPLE_LUNCH, encoding="utf-8")
    with State(LedgerPaths(config.ledger).state_db) as state:
        Pipeline(config, FakeBackend(), state).run()


def _inject_signals(config: Config) -> None:
    """The fake backend emits no signals; add some so aggregation is exercised."""
    paths = LedgerPaths(config.ledger)
    path = next(paths.meetings.rglob("*.json"))
    meeting = Meeting.model_validate_json(path.read_text(encoding="utf-8"))
    meeting.speaker_stats = SpeakerStats(attribution="reliable", mic_mode="per_speaker")
    target = next(s for s in meeting.segments if s.project_id == "alpha-migration")
    target.signals = [
        Signal(kind="resistance", topic="納期", speaker="suzuki", intensity="high", concern="テスト期間不足"),
        Signal(
            kind="repetition", topic="納期", speaker="suzuki",
            intensity="medium", concern="過去も延伸した",
        ),
        Signal(kind="concern", topic="外注費", speaker="tanaka", intensity="medium", concern="単価上昇"),
    ]
    path.write_text(meeting.model_dump_json(indent=2), encoding="utf-8")
    with State(paths.state_db) as state:
        state.index_meeting(meeting, str(path.with_suffix(".md")), str(path))


def test_context_pack_has_the_pieces_llms_need(config: Config):
    _seed(config)
    _inject_signals(config)
    pack = build_pack(config.ledger, "alpha-migration", purpose="material")
    data = pack.to_dict()
    assert data["project"]["id"] == "alpha-migration"
    assert data["recent_segments"]
    assert data["concerns"]
    assert data["hint"]
    assert "関係者が気にしていること" in pack.to_markdown()


def test_concerns_aggregate_by_person_and_topic(config: Config):
    _seed(config)
    _inject_signals(config)
    rows = aggregate_concerns(config.ledger)
    top = rows[0]
    assert top["who"] == "鈴木 花子"
    assert top["topic"] == "納期"
    assert top["count"] == 2
    assert top["max_intensity"] == "high"


def test_concerns_filter_by_person_name(config: Config):
    _seed(config)
    _inject_signals(config)
    rows = aggregate_concerns(config.ledger, speaker="田中")
    assert [r["topic"] for r in rows] == ["外注費"]


def test_search_finds_japanese_substrings(config: Config):
    _seed(config)
    with State(LedgerPaths(config.ledger).state_db) as state:
        assert state.search("採用サイト")


def test_reindex_rebuilds_from_json(config: Config, tmp_path: Path):
    _seed(config)
    paths = LedgerPaths(config.ledger)
    paths.state_db.unlink()
    cfg_file = _config_file(tmp_path, config)
    result = runner.invoke(app, ["reindex", "--config", str(cfg_file)])
    assert result.exit_code == 0, result.output
    with State(paths.state_db) as state:
        assert state.stats()["meetings"] == 1


class TestCli:
    def test_init_creates_tree(self, tmp_path: Path):
        root = tmp_path / "new-ledger"
        result = runner.invoke(app, ["init", "--ledger", str(root), "--no-git", "--no-write-config"])
        assert result.exit_code == 0, result.output
        assert (root / "registry" / "projects.yaml").is_file()

    def test_project_list_json(self, config: Config, tmp_path: Path):
        cfg_file = _config_file(tmp_path, config)
        result = runner.invoke(app, ["project", "list", "--config", str(cfg_file), "--json"])
        assert result.exit_code == 0, result.output
        ids = {p["id"] for p in json.loads(result.stdout)}
        assert ids == {"alpha-migration", "recruit-site"}

    def test_project_new_then_append(self, config: Config, tmp_path: Path):
        cfg_file = _config_file(tmp_path, config)
        result = runner.invoke(
            app, ["project", "new", "オフィス移転", "--id", "office-move", "--config", str(cfg_file)]
        )
        assert result.exit_code == 0, result.output
        result = runner.invoke(
            app,
            ["append", "office-move", "-t", "内見は9月", "-s", "logs", "--config", str(cfg_file)],
        )
        assert result.exit_code == 0, result.output
        logs = list((LedgerPaths(config.ledger).project_dir("office-move") / "logs").glob("*.md"))
        assert logs and "内見は9月" in logs[0].read_text(encoding="utf-8")

    def test_append_rejects_unknown_project(self, config: Config, tmp_path: Path):
        cfg_file = _config_file(tmp_path, config)
        result = runner.invoke(app, ["append", "nope", "-t", "x", "--config", str(cfg_file)])
        assert result.exit_code == 1

    def test_ingest_run_end_to_end(self, config: Config, tmp_path: Path):
        cfg_file = _config_file(tmp_path, config)
        (config.ingest.inbox / "2026-08-13-1200-昼会.md").write_text(SAMPLE_LUNCH, encoding="utf-8")
        result = runner.invoke(app, ["ingest", "run", "--config", str(cfg_file), "--no-commit"])
        assert result.exit_code == 0, result.output
        assert "alpha-migration" in result.output

    def test_ingest_add_stdin(self, config: Config, tmp_path: Path):
        cfg_file = _config_file(tmp_path, config)
        result = runner.invoke(
            app,
            ["ingest", "add", "-", "--kind", "chat", "-p", "recruit-site",
             "--config", str(cfg_file), "--no-commit"],
            input="佐藤: デザイン案の件、来週レビューします。\n",
        )
        assert result.exit_code == 0, result.output
        assert list(LedgerPaths(config.ledger).project_notes("recruit-site", "chat").glob("*.md"))

    def test_triage_and_assign(self, config: Config, tmp_path: Path):
        cfg_file = _config_file(tmp_path, config)
        (config.ingest.inbox / "2026-08-15-1000-雑談.md").write_text(
            "# オフィスの椅子の話\n田中: 椅子を替えたい。\n", encoding="utf-8"
        )
        runner.invoke(app, ["ingest", "run", "--config", str(cfg_file), "--no-commit"])

        result = runner.invoke(app, ["triage", "--config", str(cfg_file), "--json"])
        assert result.exit_code == 0, result.output
        rows = json.loads(result.stdout)
        assert rows, "未割当セグメントが出ていません"

        row = rows[0]
        result = runner.invoke(
            app,
            ["assign", row["meeting_id"], row["segment_id"], "recruit-site",
             "--config", str(cfg_file), "--no-commit"],
        )
        assert result.exit_code == 0, result.output
        assert not list(LedgerPaths(config.ledger).inbox.rglob("*.md"))

    def test_questions_and_actions_json(self, config: Config, tmp_path: Path):
        _seed(config)
        cfg_file = _config_file(tmp_path, config)
        for cmd in ("questions", "actions"):
            result = runner.invoke(app, [cmd, "--config", str(cfg_file), "--json"])
            assert result.exit_code == 0, result.output
            assert isinstance(json.loads(result.stdout), list)

    def test_context_command(self, config: Config, tmp_path: Path):
        _seed(config)
        cfg_file = _config_file(tmp_path, config)
        result = runner.invoke(
            app,
            ["context", "alpha-migration", "--purpose", "agenda", "--config", str(cfg_file), "--json"],
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["purpose"] == "agenda"

    def test_rerender_is_stable(self, config: Config, tmp_path: Path):
        _seed(config)
        cfg_file = _config_file(tmp_path, config)
        result = runner.invoke(app, ["rerender", "--config", str(cfg_file), "--no-commit"])
        assert result.exit_code == 0, result.output
        assert "変更 0 ファイル" in result.output
