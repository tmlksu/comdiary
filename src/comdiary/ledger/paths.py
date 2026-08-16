from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..util import ensure_dir, safe_filename

INBOX_PROJECT = "_inbox"

SECTIONS = (
    "readme",
    "adr",
    "spec",
    "notes",
    "logs",
    "open-questions",
    "handoff",
)


class LedgerPaths:
    """Everything that decides *where* a file goes, in one place."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # -- top level --------------------------------------------------------
    @property
    def registry(self) -> Path:
        return self.root / "registry"

    @property
    def meetings(self) -> Path:
        return self.root / "meetings"

    @property
    def projects(self) -> Path:
        return self.root / "projects"

    @property
    def inbox(self) -> Path:
        return self.root / "_inbox"

    @property
    def sources(self) -> Path:
        return self.root / "sources" / "raw"

    @property
    def internal(self) -> Path:
        return self.root / ".comdiary"

    @property
    def state_db(self) -> Path:
        return self.internal / "state.sqlite"

    # -- meetings ---------------------------------------------------------
    def meeting_slug(self, when: datetime, title: str, meeting_id: str) -> str:
        return f"{when:%Y-%m-%d-%H%M}-{safe_filename(title, 40)}-{meeting_id[:6]}"

    def meeting_dir(self, when: datetime) -> Path:
        return self.meetings / f"{when:%Y}" / f"{when:%m}"

    def meeting_md(self, when: datetime, slug: str) -> Path:
        return self.meeting_dir(when) / f"{slug}.md"

    def meeting_json(self, when: datetime, slug: str) -> Path:
        return self.meeting_dir(when) / f"{slug}.json"

    def source_archive(self, when: datetime, digest: str, name: str) -> Path:
        return self.sources / f"{when:%Y}" / f"{when:%m}" / f"{digest[:8]}-{safe_filename(name)}"

    # -- projects ---------------------------------------------------------
    def project_dir(self, project_id: str) -> Path:
        if project_id == INBOX_PROJECT:
            return self.inbox
        return self.projects / project_id

    def project_readme(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "README.md"

    def project_notes(self, project_id: str, kind: str = "meetings") -> Path:
        return self.project_dir(project_id) / "notes" / kind

    def project_note(self, project_id: str, kind: str, slug: str) -> Path:
        return self.project_notes(project_id, kind) / f"{slug}.md"

    def project_log(self, project_id: str, when: datetime) -> Path:
        return self.project_dir(project_id) / "logs" / f"{when:%Y-%m}.md"

    def project_open_questions(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "open-questions.md"

    def project_handoff(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "handoff.md"

    def project_adr_dir(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "adr"

    def project_spec_dir(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "spec"

    def section_path(self, project_id: str, section: str, when: datetime | None = None) -> Path:
        when = when or datetime.now()
        mapping = {
            "readme": self.project_readme(project_id),
            "open-questions": self.project_open_questions(project_id),
            "handoff": self.project_handoff(project_id),
            "logs": self.project_log(project_id, when),
        }
        if section in mapping:
            return mapping[section]
        if section == "adr":
            return self.project_adr_dir(project_id)
        if section == "spec":
            return self.project_spec_dir(project_id)
        if section == "notes":
            return self.project_notes(project_id)
        raise ValueError(f"未知のセクション: {section} (有効: {', '.join(SECTIONS)})")

    # -- scaffolding ------------------------------------------------------
    def ensure_project_tree(self, project_id: str) -> Path:
        base = ensure_dir(self.project_dir(project_id))
        for sub in ("adr", "spec", "notes/meetings", "notes/chat", "notes/mail", "logs"):
            ensure_dir(base / sub)
        return base
