"""The ingest pipeline.

Ordering here is deliberate and load-bearing:

1. archive the raw source **before** anything else, so a crash never loses input
2. compute speaker stats deterministically (used to distrust the LLM later)
3. LLM pass 1 — split into segments, guess projects
4. deterministic matching — alias/keyword first, LLM guess only above threshold
5. LLM pass 2 — per-segment extraction
6. render + write (idempotent blocks)
7. index
8. move the source to memos_done — **last**, so a failure leaves it retryable
"""

from __future__ import annotations

import re
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..config import Config
from ..ledger.paths import LedgerPaths
from ..ledger.writer import LedgerWriter, WriteResult
from ..llm.backend import LLMBackend, LLMError
from ..llm.prompts import detail_prompt, split_prompt
from ..models import DetailResponse, Meeting, Segment, SplitResponse
from ..registry.matcher import match_all
from ..registry.store import Registry
from ..transcript import apply_mic_policy, speaker_stats
from ..util import atomic_write, ensure_dir, now, short_hash
from .sources import SourceDoc, load_doc
from .state import State

#: Segments this short are almost always a mis-split; extracting detail for them
#: costs an LLM call and yields noise.
MIN_SEGMENT_TITLE = 2

#: How far into the document to look for a meeting date. Transcripts often state
#: it in a header block, but that block can also carry a title, a link and a
#: participant list first — so the window has to clear all of that.
DATE_SCAN_CHARS = 2000

_DATE_PATTERNS = (
    # Most specific first — a date-only match would silently discard the time.
    re.compile(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})[日]?[^\d]{0,6}(\d{1,2})[:時](\d{2})"),
    re.compile(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})[日]?[-_T ](\d{2})(\d{2})(?!\d)"),
    re.compile(r"(20\d{2})(\d{2})(\d{2})[-_T](\d{2})(\d{2})"),
    re.compile(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})"),
    re.compile(r"(20\d{2})(\d{2})(\d{2})"),
)

#: A time on its own, but only when it is clearly labelled as the meeting's.
#: A bare ``\d{1,2}:\d{2}`` would happily match a per-utterance timestamp
#: ("00:03:12 田中: ...") and silently date the meeting to 00:03.
_LABELLED_TIME = re.compile(
    r"(?:日時|時刻|開始|開始時刻|時間|date|time|when)[^\n]{0,20}?[:：][^\n]{0,20}?"
    r"(?<!\d)(\d{1,2})\s*[:時]\s*(\d{2})(?!\d)",
    re.IGNORECASE,
)


@dataclass
class DatedAt:
    when: datetime
    source: str
    #: Where the clock time came from. ``None`` means only a date was found and
    #: ``when`` is midnight — a placeholder, not an observation.
    time_source: str | None = None

    @property
    def has_time(self) -> bool:
        return self.time_source is not None


def _scan(text: str) -> tuple[datetime, bool] | None:
    """First date in ``text``, and whether it carried a clock time."""
    for pattern in _DATE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        parts = [int(g) for g in m.groups()]
        try:
            if len(parts) == 5:
                return datetime(parts[0], parts[1], parts[2], parts[3], parts[4]), True
            return datetime(parts[0], parts[1], parts[2]), False
        except ValueError:
            continue
    return None


def _labelled_time(text: str) -> tuple[int, int] | None:
    for m in _LABELLED_TIME.finditer(text):
        hour, minute = int(m.group(1)), int(m.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    return None


def guess_datetime(doc: SourceDoc, llm_date: str | None = None) -> DatedAt:
    """Resolve the meeting timestamp, taking the date and the time separately.

    Filenames commonly carry ``yyyy-mm-dd`` and no clock time. Trusting the
    filename wholesale would then stamp every meeting that day at 00:00 and
    destroy the ordering between them, so once a date is fixed we keep looking
    for a time — in the body, then from the LLM — and adopt one only if it
    belongs to the same calendar day.

    ``mtime`` is the last resort and is reported as such: when a transcript is
    published after the fact (a notification arrives, a job fetches and converts
    it), the file's write time is *not* the meeting time and can be off by
    hours or days. Callers surface that so it is never a silent error.
    """
    body = doc.text[:DATE_SCAN_CHARS]
    candidates = ((doc.name, "filename"), (body, "body"), (llm_date or "", "llm"))
    scanned = [(source, _scan(text)) for text, source in candidates]

    chosen: DatedAt | None = None
    for source, hit in scanned:
        if hit is None:
            continue
        when, has_time = hit
        chosen = DatedAt(when.astimezone(), source, source if has_time else None)
        break

    if chosen is None and llm_date:
        try:
            parsed = datetime.fromisoformat(llm_date.replace("Z", "+00:00")).astimezone()
        except ValueError:
            parsed = None
        if parsed:
            chosen = DatedAt(parsed, "llm", "llm")

    if chosen is None:
        if doc.mtime:
            stamp = datetime.fromtimestamp(doc.mtime).astimezone()
            return DatedAt(stamp, "mtime", "mtime")
        return DatedAt(now(), "unknown", None)

    if chosen.has_time:
        return chosen
    return _borrow_time(chosen, scanned, body)


def _borrow_time(chosen: DatedAt, scanned: list, body: str) -> DatedAt:
    """Fill in a missing clock time from a source that agrees on the date.

    The same-day check matters: a transcript body is full of other dates and
    times (deadlines, next meetings), and none of them are this meeting's start.
    """
    for source, hit in scanned:
        if hit is None:
            continue
        when, has_time = hit
        if has_time and when.date() == chosen.when.date():
            return DatedAt(chosen.when.replace(hour=when.hour, minute=when.minute), chosen.source, source)

    found = _labelled_time(body)
    if found:
        return DatedAt(chosen.when.replace(hour=found[0], minute=found[1]), chosen.source, "body")
    return chosen


@dataclass
class IngestOutcome:
    doc: SourceDoc
    meeting: Meeting | None = None
    slug: str = ""
    write: WriteResult | None = None
    error: str = ""
    skipped: str = ""
    llm_calls: int = 0

    @property
    def ok(self) -> bool:
        return self.meeting is not None and not self.error


@dataclass
class RunReport:
    outcomes: list[IngestOutcome] = field(default_factory=list)
    warming: list[Path] = field(default_factory=list)
    dry_run: bool = False

    @property
    def processed(self) -> int:
        return sum(1 for o in self.outcomes if o.ok)

    @property
    def failed(self) -> int:
        return sum(1 for o in self.outcomes if o.error)


class Pipeline:
    def __init__(
        self,
        config: Config,
        llm: LLMBackend,
        state: State,
        registry: Registry | None = None,
        dry_run: bool = False,
    ) -> None:
        self.config = config
        self.llm = llm
        self.state = state
        self.paths = LedgerPaths(config.ledger)
        self.registry = registry or Registry.load(config.ledger)
        self.dry_run = dry_run
        self.writer = LedgerWriter(self.paths, dry_run=dry_run, registry=self.registry)

    # -- one document -----------------------------------------------------
    def ingest_doc(self, doc: SourceDoc, project_hint: str | None = None) -> IngestOutcome:
        outcome = IngestOutcome(doc=doc)
        if self.state.is_done(doc.sha256):
            outcome.skipped = "同一内容を取り込み済み"
            return outcome
        if not doc.text.strip():
            outcome.error = "本文が空です"
            return outcome

        try:
            meeting = self._build_meeting(doc, outcome, project_hint)
        except LLMError as exc:
            outcome.error = f"LLM: {exc}"
            return outcome
        except Exception as exc:  # noqa: BLE001 - the run must survive one bad file
            outcome.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}"
            return outcome

        slug = self.paths.meeting_slug(meeting.date, meeting.title, meeting.meeting_id)
        outcome.slug = slug
        outcome.meeting = meeting

        for pid in meeting.project_ids():
            project = self.registry.project(pid)
            if project:
                self.writer.ensure_project(project)

        outcome.write = self.writer.write_meeting(meeting, slug)
        if not self.dry_run and outcome.write.meeting_md:
            self.state.index_meeting(
                meeting,
                doc=str(outcome.write.meeting_md),
                json_path=str(outcome.write.meeting_json),
            )
        return outcome

    def _build_meeting(
        self, doc: SourceDoc, outcome: IngestOutcome, project_hint: str | None
    ) -> Meeting:
        stats = speaker_stats(doc.text)
        meeting_id = short_hash(doc.sha256, 12)

        hint = ""
        if project_hint:
            project = self.registry.project(project_hint)
            name = project.name if project else project_hint
            hint = (
                f"## 補足\nこの文書は案件「{name}」(id: {project_hint}) に属すると"
                "人間から指定されています。guess.project_id にはこの id を使ってください。\n"
            )

        split: SplitResponse = self.llm.complete_json(
            split_prompt(doc.text, self.registry, stats, hint), SplitResponse
        )
        outcome.llm_calls += 1

        dated = guess_datetime(doc, split.date)
        when = dated.when
        segments = [s for s in split.segments if len(s.title.strip()) >= MIN_SEGMENT_TITLE]
        for i, seg in enumerate(segments, start=1):
            if not seg.segment_id:
                seg.segment_id = f"s{i}"

        if project_hint and self.registry.project(project_hint):
            for seg in segments:
                seg.project_id = project_hint
                seg.match_method = "manual"
        match_all(segments, self.registry, self.config.match.min_confidence)

        # Existing vocabulary goes back into the prompt so labels converge
        # rather than fragmenting; aggregation is the whole point of topics.
        known_topics = self.state.known_topics()
        for seg in segments:
            self._extract_detail(doc, seg, stats, outcome, known_topics)
            apply_mic_policy(seg.signals, stats)
            for topic in seg.topics:
                if topic not in known_topics:
                    known_topics.append(topic)

        archive = self._archive(doc, when)
        return Meeting(
            meeting_id=meeting_id,
            title=split.title.strip() or doc.path.stem,
            kind=doc.kind,  # type: ignore[arg-type]
            date=when,
            date_source=dated.source,  # type: ignore[arg-type]
            time_source=dated.time_source,  # type: ignore[arg-type]
            attendees=split.attendees,
            summary=split.summary,
            source_path=str(doc.path),
            source_sha256=doc.sha256,
            source_archive=str(archive) if archive else "",
            ingested_at=now(),
            speaker_stats=stats,
            segments=segments,
            llm={"backend": getattr(self.llm, "name", "?"), "model": self.config.llm.model},
        )

    def _extract_detail(
        self,
        doc: SourceDoc,
        seg: Segment,
        stats,
        outcome: IngestOutcome,
        known_topics: list[str] | None = None,
    ) -> None:
        project = self.registry.project(seg.project_id) if seg.project_id else None
        detail: DetailResponse = self.llm.complete_json(
            detail_prompt(
                doc.text,
                seg.title,
                seg.span,
                project.name if project else None,
                self.registry,
                stats,
                known_topics,
            ),
            DetailResponse,
        )
        outcome.llm_calls += 1
        if detail.summary:
            seg.summary = detail.summary
        seg.topics = [t.strip() for t in detail.topics if t.strip()][:5]
        seg.decisions = detail.decisions
        seg.actions = detail.actions
        seg.open_questions = detail.open_questions
        seg.signals = detail.signals
        seg.risks = detail.risks
        seg.next_agenda = detail.next_agenda
        seg.detail_extracted = True

    def _archive(self, doc: SourceDoc, when: datetime) -> Path | None:
        target = self.paths.source_archive(when, doc.sha256, doc.name)
        if self.dry_run:
            return target
        if not target.exists():
            atomic_write(target, doc.text)
        return target

    # -- the scheduled run ------------------------------------------------
    def run(self, limit: int | None = None) -> RunReport:
        from .sources import discover

        cfg = self.config.ingest
        if cfg.inbox is None:
            raise ValueError("comdiary.toml の [ingest] inbox が未設定です")

        ready, warming = discover(
            cfg.inbox,
            glob=cfg.glob,
            limit=limit if limit is not None else cfg.limit,
            quiet_seconds=cfg.quiet_seconds,
        )
        report = RunReport(warming=warming, dry_run=self.dry_run)
        run_id = None if self.dry_run else self.state.start_run()

        for path in ready:
            doc = load_doc(path)
            outcome = self.ingest_doc(doc)
            report.outcomes.append(outcome)
            if self.dry_run:
                continue
            self._record(outcome)
            self._move_source(outcome)

        if run_id is not None:
            self.state.end_run(run_id, report.processed, report.failed)
        return report

    def _record(self, outcome: IngestOutcome) -> None:
        doc = outcome.doc
        if outcome.skipped:
            status, meeting_id, md, err = "skipped", None, None, outcome.skipped
        elif outcome.error:
            status, meeting_id, md, err = "failed", None, None, outcome.error
        else:
            assert outcome.meeting and outcome.write
            status = "ok"
            meeting_id = outcome.meeting.meeting_id
            md = str(outcome.write.meeting_md)
            err = None
        self.state.record_source(
            doc.sha256, doc.path, doc.name, doc.kind, status, meeting_id, md, err
        )

    def _move_source(self, outcome: IngestOutcome) -> None:
        """Move only after everything else succeeded — a failure stays retryable."""
        cfg = self.config.ingest
        src = outcome.doc.path
        if not src.is_file():
            return
        if outcome.error:
            dest_dir = cfg.failed
        elif outcome.ok or outcome.skipped:
            dest_dir = cfg.done
        else:
            return
        if dest_dir is None:
            return
        ensure_dir(dest_dir)
        dest = dest_dir / src.name
        if dest.exists():
            dest = dest_dir / f"{src.stem}-{outcome.doc.sha256[:8]}{src.suffix}"
        if outcome.error and cfg.failed:
            (dest.with_suffix(dest.suffix + ".error.txt")).write_text(
                outcome.error, encoding="utf-8"
            )
        try:
            src.replace(dest)
        except OSError:
            # memos_done may sit on a different volume (Drive stream vs. local).
            import shutil

            shutil.move(str(src), str(dest))
