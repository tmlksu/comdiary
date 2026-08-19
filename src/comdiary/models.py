"""Canonical data model for comdiary.

Design rule that everything else depends on: the LLM only ever produces these
objects (validated JSON). Markdown is *rendered* from them by deterministic
code. That keeps the ledger re-renderable, diffable and idempotent.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1


class Base(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

ProjectStatus = Literal["active", "paused", "closed"]


class Project(Base):
    id: str = Field(description="ASCII slug, stable forever. e.g. 'alpha-migration'")
    name: str
    status: ProjectStatus = "active"
    aliases: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    members: list[str] = Field(default_factory=list, description="person ids")
    summary: str = ""
    started: date | None = None
    closed: date | None = None


class Person(Base):
    id: str = Field(description="ASCII slug. e.g. 'tanaka'")
    name: str
    aliases: list[str] = Field(default_factory=list)
    role: str = ""
    org: str = ""


# --------------------------------------------------------------------------
# extraction payloads
# --------------------------------------------------------------------------

# Fixed vocabulary on purpose. Free-text "temperature" cannot be aggregated
# later, and aggregation is the whole point (see `comdiary concerns`).
SignalKind = Literal[
    "escalation",  # 声を荒げた / 語気が強い
    "resistance",  # 反対・押し返し
    "enthusiasm",  # 前のめり・強い賛意
    "hesitation",  # 言い淀み・条件付き同意
    "silence",  # 本来発言しそうな人が黙った
    "repetition",  # 同じ主張を繰り返した = 重要度が高い
    "deflection",  # 明言を避けた・話題を逸らした
    "concern",  # 明示的な懸念表明(温度は高くないが記録価値あり)
]

Intensity = Literal["low", "medium", "high"]


class Signal(Base):
    """A noteworthy *emotional / rhetorical* observation, not a fact.

    Only recorded when it is genuinely notable ("特筆すべきなら"), so an empty
    list is a valid and common outcome.
    """

    kind: SignalKind
    topic: str = Field(description="何について。短い名詞句")
    speaker: str | None = Field(
        default=None,
        description="person id か表記名。話者推定が不確かなら null にすること",
    )
    intensity: Intensity = "medium"
    evidence: str = Field(default="", description="どこでどう表れたかの客観的記述")
    quote: str | None = Field(default=None, description="逐語引用(あれば)")
    concern: str | None = Field(default=None, description="根底にある関心事・懸念")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class Decision(Base):
    what: str
    rationale: str = ""
    decided_by: list[str] = Field(default_factory=list)
    reversible: bool | None = None
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)


class ActionItem(Base):
    what: str
    owner: str | None = None
    due: str | None = Field(default=None, description="ISO date か '来週' 等の原文")
    status: Literal["open", "done", "dropped"] = "open"
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)


class OpenQuestion(Base):
    question: str
    raised_by: str | None = None
    blocks: str = Field(default="", description="これが決まらないと何が止まるか")
    status: Literal["open", "answered", "obsolete"] = "open"


class Risk(Base):
    what: str
    impact: Intensity = "medium"
    raised_by: str | None = None


class ProjectGuess(Base):
    project_id: str | None = Field(
        default=None, description="registry に存在する id のみ。無ければ null"
    )
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = ""
    suggested_name: str | None = Field(
        default=None, description="既存案件に該当しない場合の新規案件名の提案"
    )


class Segment(Base):
    """One topic-coherent chunk of a meeting.

    A single 昼会 routinely covers several projects; the segment is the unit
    that gets projected onto a project's ledger.
    """

    segment_id: str = Field(description="'s1', 's2', ... 会議内で一意")
    title: str
    summary: str = ""
    #: Short noun phrases naming what this segment is *about*. The aggregation
    #: key for `comdiary topics`, which is how a recurring concern becomes
    #: visible before anyone has decided it is a project. Deliberately separate
    #: from Signal.topic: signals are only recorded when something is notable,
    #: so relying on them would surface only the loud subjects.
    topics: list[str] = Field(default_factory=list)
    span: str = Field(default="", description="転記上の位置。見出し・時刻・冒頭の一文など")
    speakers: list[str] = Field(default_factory=list)
    guess: ProjectGuess = Field(default_factory=ProjectGuess)
    # filled in by the matcher, not by the LLM
    project_id: str | None = None
    match_method: Literal["alias", "keyword", "llm", "manual", "unmatched"] = "unmatched"

    decisions: list[Decision] = Field(default_factory=list)
    actions: list[ActionItem] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    signals: list[Signal] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    next_agenda: list[str] = Field(default_factory=list)
    detail_extracted: bool = False


#: Where a meeting's timestamp came from, most trustworthy first. Recorded
#: because "mtime" means the file's write time, which for a transcript published
#: after the fact (mail notification -> fetch -> convert) is not the meeting time.
DateSource = Literal["filename", "body", "llm", "mtime", "unknown"]

MicMode = Literal["shared", "per_speaker", "unknown"]


class SpeakerStats(Base):
    """Computed deterministically from the transcript, never by the LLM."""

    distribution: dict[str, float] = Field(default_factory=dict)
    line_counts: dict[str, int] = Field(default_factory=dict)
    total_lines: int = 0
    mic_mode: MicMode = "unknown"
    attribution: Literal["reliable", "uncertain"] = "uncertain"
    note: str = ""


class Meeting(Base):
    """The canonical record of one ingested source document."""

    schema_version: int = SCHEMA_VERSION
    meeting_id: str
    title: str
    kind: Literal["meeting", "chat", "mail", "note"] = "meeting"
    date: datetime
    date_source: DateSource = "unknown"
    #: None means only a date was known and `date`'s time is a 00:00 placeholder.
    time_source: DateSource | None = None
    attendees: list[str] = Field(default_factory=list)
    summary: str = ""
    source_path: str = ""
    source_sha256: str = ""
    source_archive: str = ""
    ingested_at: datetime | None = None
    speaker_stats: SpeakerStats = Field(default_factory=SpeakerStats)
    segments: list[Segment] = Field(default_factory=list)
    llm: dict[str, str] = Field(default_factory=dict, description="backend/model の記録")

    def project_ids(self) -> list[str]:
        seen: list[str] = []
        for s in self.segments:
            if s.project_id and s.project_id not in seen:
                seen.append(s.project_id)
        return seen


# --------------------------------------------------------------------------
# LLM response envelopes (what the model is actually asked to emit)
# --------------------------------------------------------------------------


class SplitResponse(Base):
    """Pass 1: meeting-level metadata + segment outline."""

    title: str
    date: str | None = Field(default=None, description="ISO8601。読み取れなければ null")
    attendees: list[str] = Field(default_factory=list)
    summary: str = ""
    segments: list[Segment] = Field(default_factory=list)


class DetailResponse(Base):
    """Pass 2: deep extraction for a single segment."""

    summary: str = ""
    topics: list[str] = Field(
        default_factory=list, description="この話題を表す短い名詞句を1-3個"
    )
    decisions: list[Decision] = Field(default_factory=list)
    actions: list[ActionItem] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    signals: list[Signal] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    next_agenda: list[str] = Field(default_factory=list)
