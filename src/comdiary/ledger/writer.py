"""Project a Meeting onto the ledger.

The Meeting JSON is canonical; every markdown file here is a *projection* of it
and can be rebuilt with `comdiary rerender`. Machine-written regions are fenced
with comdiary blocks so human prose in the same file survives untouched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..models import Meeting, Project, Segment
from ..util import atomic_write
from . import blocks
from .paths import LedgerPaths
from .render import (
    relpath,
    render_inbox_segment,
    render_log_entry,
    render_meeting,
    render_open_questions,
    render_project_readme,
    render_segment_note,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..registry.store import Registry

LOG_HEADER = """\
---
comdiary: log
---

# 月次ログ

<!-- comdiary が会議ごとに追記します。人の追記は blocks の外に書いてください。 -->
"""

OQ_HEADER = """\
---
comdiary: open-questions
---

# 未解決の論点

<!-- comdiary が会議ごとに追記します。解決したら該当行を編集/削除してください。 -->
"""

HANDOFF_HEADER = """\
---
comdiary: handoff
---

# 引き継ぎメモ

<!-- ここは人が書きます。「いま誰かに引き継ぐなら何を伝えるか」。 -->
"""


@dataclass
class WriteResult:
    meeting_md: Path | None = None
    meeting_json: Path | None = None
    written: list[Path] = field(default_factory=list)
    changed: list[Path] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)

    def note(self, path: Path, changed: bool) -> None:
        self.written.append(path)
        if changed:
            self.changed.append(path)


class LedgerWriter:
    def __init__(
        self,
        paths: LedgerPaths,
        dry_run: bool = False,
        registry: Registry | None = None,
    ) -> None:
        self.paths = paths
        self.dry_run = dry_run
        # id -> display name. The LLM writes registry ids into the canonical
        # JSON; documents a human reads should show the actual names.
        self.names: dict[str, str] = (
            {p.id: p.name for p in registry.people} if registry else {}
        )

    # -- low level --------------------------------------------------------
    def _write(self, path: Path, content: str) -> bool:
        if path.is_file() and path.read_text(encoding="utf-8") == content:
            return False
        if not self.dry_run:
            atomic_write(path, content)
        return True

    def _upsert(self, path: Path, key: str, body: str, header: str) -> bool:
        exists = path.is_file()
        text = path.read_text(encoding="utf-8") if exists else header
        new_text, changed = blocks.upsert(text, key, body)
        if not self.dry_run and (changed or not exists):
            atomic_write(path, new_text)
        return changed

    # -- public -----------------------------------------------------------
    def ensure_project(self, project: Project) -> Path:
        base = self.paths.project_dir(project.id)
        if self.dry_run:
            return base
        self.paths.ensure_project_tree(project.id)
        readme = self.paths.project_readme(project.id)
        if not readme.is_file():
            atomic_write(readme, render_project_readme(project))
        for path, header in (
            (self.paths.project_open_questions(project.id), OQ_HEADER),
            (self.paths.project_handoff(project.id), HANDOFF_HEADER),
        ):
            if not path.is_file():
                atomic_write(path, header)
        return base

    def write_meeting(self, meeting: Meeting, slug: str) -> WriteResult:
        result = WriteResult()
        md_path = self.paths.meeting_md(meeting.date, slug)
        json_path = self.paths.meeting_json(meeting.date, slug)

        result.meeting_json = json_path
        result.note(json_path, self._write(json_path, meeting.model_dump_json(indent=2) + "\n"))
        result.meeting_md = md_path
        result.note(md_path, self._write(md_path, render_meeting(meeting, self.names)))

        for segment in meeting.segments:
            if segment.project_id:
                self._project_segment(meeting, segment, slug, md_path, result)
            else:
                self._inbox_segment(meeting, segment, slug, md_path, result)
                result.unmatched.append(segment.segment_id)
        return result

    # -- segment projections ---------------------------------------------
    def _note_slug(self, slug: str, segment: Segment) -> str:
        return f"{slug}--{segment.segment_id}"

    def _project_segment(
        self, meeting: Meeting, segment: Segment, slug: str, md_path: Path, result: WriteResult
    ) -> None:
        pid = segment.project_id
        assert pid is not None
        if not self.dry_run:
            self.paths.ensure_project_tree(pid)

        note_kind = {"meeting": "meetings", "chat": "chat", "mail": "mail", "note": "meetings"}[
            meeting.kind
        ]
        note_path = self.paths.project_note(pid, note_kind, self._note_slug(slug, segment))
        meeting_rel = relpath(md_path, note_path.parent)
        result.note(
            note_path,
            self._write(note_path, render_segment_note(meeting, segment, pid, meeting_rel, self.names)),
        )

        log_path = self.paths.project_log(pid, meeting.date)
        key = f"meeting/{meeting.meeting_id}/{segment.segment_id}"
        body = render_log_entry(
            meeting,
            segment,
            relpath(note_path, log_path.parent),
            relpath(md_path, log_path.parent),
            self.names,
        )
        result.note(log_path, self._upsert(log_path, key, body, LOG_HEADER))

        open_qs = [q for q in segment.open_questions if q.status == "open"]
        if open_qs:
            oq_path = self.paths.project_open_questions(pid)
            oq_body = render_open_questions(
                meeting, open_qs, relpath(md_path, oq_path.parent), self.names
            )
            result.note(oq_path, self._upsert(oq_path, key, oq_body, OQ_HEADER))

    def _inbox_segment(
        self, meeting: Meeting, segment: Segment, slug: str, md_path: Path, result: WriteResult
    ) -> None:
        path = (
            self.paths.inbox
            / f"{meeting.date:%Y-%m}"
            / f"{self._note_slug(slug, segment)}.md"
        )
        result.note(
            path,
            self._write(
                path,
                render_inbox_segment(meeting, segment, relpath(md_path, path.parent), self.names),
            ),
        )


def scaffold(paths: LedgerPaths) -> list[Path]:
    """Create an empty but complete ledger tree."""
    from ..util import ensure_dir

    created: list[Path] = []
    for d in (paths.registry, paths.meetings, paths.projects, paths.inbox, paths.sources, paths.internal):
        if not d.exists():
            created.append(d)
        ensure_dir(d)

    seeds = {
        paths.registry / "projects.yaml": (
            "# comdiary 案件台帳\n"
            "# id は ASCII の slug、以後変更しないこと。\n"
            "# - id: alpha-migration\n"
            "#   name: Alpha基盤移行\n"
            "#   status: active\n"
            "#   summary: 既存基盤から新基盤への移行\n"
            "#   aliases: [アルファ, 基盤移行]\n"
            "#   keywords: [切替日, 移行計画]\n"
            "#   members: [tanaka]\n"
            "[]\n"
        ),
        paths.registry / "people.yaml": (
            "# comdiary 人物台帳\n"
            "# - id: tanaka\n"
            "#   name: 田中 太郎\n"
            "#   aliases: [田中, Tanaka]\n"
            "#   role: PM\n"
            "[]\n"
        ),
        paths.registry / "glossary.yaml": (
            "# 社内用語。LLM に毎回渡されるので、略語や独自用語を書いておくと精度が上がります。\n"
            "# 例: {}\n"
        ),
        # .comdiary/ holds only derived state (index) and the run lock — both are
        # rebuildable, and committing a lock file mid-run would be nonsense.
        paths.root / ".gitignore": ".comdiary/\n",
        paths.root
        / "README.md": (
            "# 台帳 (comdiary ledger)\n\n"
            "会社の会議・チャット・メールの docs-as-code 台帳です。\n\n"
            "**このリポジトリにリモートを設定しないでください。** ローカル専用です。\n\n"
            "- `registry/` — 案件台帳・人物台帳・用語集(人が編集)\n"
            "- `meetings/` — 会議ごとの正本(comdiary が生成。手で編集しない)\n"
            "- `projects/<id>/` — 案件ごとの living docs\n"
            "- `_inbox/` — 案件未確定のセグメント。`comdiary triage` で割り当てる\n"
            "- `sources/raw/` — 取り込んだ原文のアーカイブ\n"
        ),
    }
    for path, content in seeds.items():
        if not path.exists():
            atomic_write(path, content)
            created.append(path)
    return created
