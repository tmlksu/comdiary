from __future__ import annotations

from pathlib import Path

from comdiary.config import Config
from comdiary.ingest.pipeline import Pipeline, guess_datetime
from comdiary.ingest.sources import SourceDoc, discover
from comdiary.ingest.state import State
from comdiary.ledger.paths import LedgerPaths
from comdiary.llm.fake import FakeBackend
from comdiary.models import Meeting

from .conftest import SAMPLE_LUNCH, SAMPLE_SHARED_MIC


def write_memo(config: Config, name: str, text: str) -> Path:
    path = config.ingest.inbox / name
    path.write_text(text, encoding="utf-8")
    return path


def run(config: Config, **kwargs):
    with State(LedgerPaths(config.ledger).state_db) as state:
        pipeline = Pipeline(config, FakeBackend(), state, **kwargs)
        return pipeline.run()


def test_lunch_meeting_splits_across_projects(config: Config):
    write_memo(config, "2026-08-13-1200-昼会.md", SAMPLE_LUNCH)
    report = run(config)

    assert report.processed == 1
    meeting = report.outcomes[0].meeting
    assert meeting is not None
    assert len(meeting.segments) == 2
    assert set(meeting.project_ids()) == {"alpha-migration", "recruit-site"}


def test_projection_lands_in_each_project(config: Config):
    write_memo(config, "2026-08-13-1200-昼会.md", SAMPLE_LUNCH)
    run(config)
    paths = LedgerPaths(config.ledger)

    for pid in ("alpha-migration", "recruit-site"):
        notes = list(paths.project_notes(pid, "meetings").glob("*.md"))
        assert notes, f"{pid} に会議メモが作られていません"
        log = paths.project_log(pid, __import__("datetime").datetime(2026, 8, 13))
        assert log.is_file()
        assert "comdiary:begin" in log.read_text(encoding="utf-8")


def test_meeting_master_record_is_written_once(config: Config):
    write_memo(config, "2026-08-13-1200-昼会.md", SAMPLE_LUNCH)
    run(config)
    paths = LedgerPaths(config.ledger)
    mds = list(paths.meetings.rglob("*.md"))
    jsons = list(paths.meetings.rglob("*.json"))
    assert len(mds) == 1 and len(jsons) == 1
    meeting = Meeting.model_validate_json(jsons[0].read_text(encoding="utf-8"))
    assert meeting.source_sha256


def test_source_is_moved_to_done(config: Config):
    path = write_memo(config, "2026-08-13-1200-昼会.md", SAMPLE_LUNCH)
    run(config)
    assert not path.exists()
    assert (config.ingest.done / path.name).is_file()


def test_reingest_is_idempotent(config: Config):
    write_memo(config, "a.md", SAMPLE_LUNCH)
    run(config)
    paths = LedgerPaths(config.ledger)
    before = {p: p.read_text(encoding="utf-8") for p in paths.root.rglob("*.md")}

    write_memo(config, "b.md", SAMPLE_LUNCH)  # same content, different name
    report = run(config)
    assert report.outcomes[0].skipped

    after = {p: p.read_text(encoding="utf-8") for p in paths.root.rglob("*.md")}
    assert before.keys() == after.keys()
    assert before == after


def test_dry_run_writes_nothing(config: Config):
    write_memo(config, "2026-08-13-1200-昼会.md", SAMPLE_LUNCH)
    report = run(config, dry_run=True)
    paths = LedgerPaths(config.ledger)
    assert report.outcomes[0].ok
    assert not list(paths.meetings.rglob("*.md"))
    assert list(config.ingest.inbox.glob("*.md"))  # source left in place


def test_shared_mic_meeting_drops_speaker_attribution(config: Config):
    write_memo(config, "2026-08-14-1000-定例.md", SAMPLE_SHARED_MIC)
    report = run(config)
    meeting = report.outcomes[0].meeting
    assert meeting is not None
    assert meeting.speaker_stats.mic_mode == "shared"
    assert all(s.speaker is None for seg in meeting.segments for s in seg.signals)


def test_unmatched_segment_goes_to_inbox(config: Config):
    write_memo(config, "2026-08-15-1000-雑談.md", "# 全く新しい話題\n田中: オフィスの椅子を替えたい。\n")
    report = run(config)
    paths = LedgerPaths(config.ledger)
    assert report.outcomes[0].write is not None
    assert report.outcomes[0].write.unmatched
    assert list(paths.inbox.rglob("*.md"))


def test_failed_source_moves_to_failed_dir(config: Config):
    class Broken(FakeBackend):
        def complete_json(self, prompt, schema):
            raise RuntimeError("模擬的な失敗")

    path = write_memo(config, "broken.md", SAMPLE_LUNCH)
    with State(LedgerPaths(config.ledger).state_db) as state:
        Pipeline(config, Broken(), state).run()
    assert not path.exists()
    assert (config.ingest.failed / "broken.md").is_file()
    assert (config.ingest.failed / "broken.md.error.txt").is_file()


def test_quiet_seconds_defers_files_still_being_written(config: Config, tmp_path: Path):
    write_memo(config, "warm.md", SAMPLE_LUNCH)
    ready, warming = discover(config.ingest.inbox, "*.md", limit=5, quiet_seconds=3600)
    assert not ready and len(warming) == 1


def test_limit_is_respected(config: Config):
    for i in range(4):
        write_memo(config, f"m{i}.md", SAMPLE_LUNCH.replace("昼会", f"昼会{i}"))
    with State(LedgerPaths(config.ledger).state_db) as state:
        report = Pipeline(config, FakeBackend(), state).run(limit=2)
    assert len(report.outcomes) == 2


def test_project_hint_pins_every_segment(config: Config):
    doc = SourceDoc(
        path=Path("chat.md"),
        name="chat.md",
        text=SAMPLE_LUNCH,
        sha256="deadbeef" * 8,
        kind="chat",
        mtime=0.0,
    )
    with State(LedgerPaths(config.ledger).state_db) as state:
        outcome = Pipeline(config, FakeBackend(), state).ingest_doc(doc, project_hint="recruit-site")
    assert outcome.meeting is not None
    assert {s.project_id for s in outcome.meeting.segments} == {"recruit-site"}


def test_chat_kind_lands_in_chat_notes(config: Config):
    doc = SourceDoc(
        path=Path("slack.md"),
        name="slack.md",
        text=SAMPLE_LUNCH,
        sha256="a" * 64,
        kind="chat",
        mtime=0.0,
    )
    with State(LedgerPaths(config.ledger).state_db) as state:
        Pipeline(config, FakeBackend(), state).ingest_doc(doc, project_hint="recruit-site")
    paths = LedgerPaths(config.ledger)
    assert list(paths.project_notes("recruit-site", "chat").glob("*.md"))


class TestDateGuess:
    def _doc(self, name: str, text: str = "", mtime: float = 0.0) -> SourceDoc:
        return SourceDoc(path=Path(name), name=name, text=text, sha256="x", mtime=mtime)

    def test_filename_with_time(self):
        got = guess_datetime(self._doc("2026-08-13-1200-昼会.md"))
        dt = got.when
        assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 8, 13, 12, 0)
        assert got.source == "filename"

    def test_filename_date_only(self):
        got = guess_datetime(self._doc("2026-08-13_昼会.md"))
        assert (got.when.year, got.when.month, got.when.day) == (2026, 8, 13)
        assert got.source == "filename"

    def test_compact_filename(self):
        got = guess_datetime(self._doc("20260813T0930_meet.md"))
        dt = got.when
        assert (dt.year, dt.month, dt.day, dt.hour) == (2026, 8, 13, 9)

    def test_japanese_date_in_body(self):
        got = guess_datetime(self._doc("memo.md", "日時: 2026年8月13日 14:30 より"))
        dt = got.when
        assert (dt.month, dt.day, dt.hour, dt.minute) == (8, 13, 14, 30)
        assert got.source == "body"

    def test_filename_beats_body(self):
        got = guess_datetime(self._doc("2026-08-13-1200-x.md", "日時: 2020年1月1日 09:00"))
        assert (got.when.year, got.when.month) == (2026, 8)
        assert got.source == "filename"

    def test_llm_date_used_when_nothing_else_has_one(self):
        got = guess_datetime(self._doc("memo.md", "日付なし"), llm_date="2026-08-13T14:30:00")
        assert (got.when.month, got.when.day) == (8, 13)
        assert got.source == "llm"

    def test_mtime_fallback_is_flagged(self):
        """The caller must be able to tell — a write time is not a meeting time."""
        got = guess_datetime(self._doc("memo.md", "日付なし", mtime=1_760_000_000.0))
        assert got.when.year >= 2025
        assert got.source == "mtime"

    def test_invalid_date_does_not_crash(self):
        got = guess_datetime(self._doc("2026-13-45-memo.md", "本文", mtime=1_760_000_000.0))
        assert got.when.year >= 2025
        assert got.source == "mtime"


def test_meeting_records_where_its_date_came_from(config: Config):
    write_memo(config, "2026-08-13-1200-昼会.md", SAMPLE_LUNCH)
    report = run(config)
    assert report.outcomes[0].meeting.date_source == "filename"


def test_undated_source_is_marked_as_mtime_derived(config: Config):
    """A transcript published after the fact carries the writer's clock, not the
    meeting's — the ledger has to say so rather than quietly assert a date."""
    write_memo(config, "議事メモ.md", "# 打ち合わせ\n田中: 進めましょう。\n")
    report = run(config)
    meeting = report.outcomes[0].meeting
    assert meeting.date_source == "mtime"

    paths = LedgerPaths(config.ledger)
    md = next(paths.meetings.rglob("*.md")).read_text(encoding="utf-8")
    assert "date_source: mtime" in md
    assert "会議の実時刻とは限りません" in md


def test_date_in_a_long_header_block_is_still_found():
    """A converted document can carry a title, a link and a participant list
    before it states the date; the scan window has to clear all of it."""
    header = "# 定例ミーティング\n\n" + "参加者: 田中 太郎, 鈴木 花子\n" * 40
    doc = SourceDoc(
        path=Path("memo.md"),
        name="memo.md",
        text=header + "日時: 2026年8月13日 14:30\n\n田中: 始めます。\n",
        sha256="x",
        mtime=1_760_000_000.0,
    )
    got = guess_datetime(doc)
    assert (got.when.month, got.when.day, got.when.hour) == (8, 13, 14)
    assert got.source == "body"
