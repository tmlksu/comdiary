"""The registries: projects.yaml / people.yaml / glossary.yaml.

These are the human-owned source of truth. comdiary reads them constantly and
writes them only on explicit commands (`project new`, `triage`), never during
an unattended ingest run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from ..models import Person, Project
from ..util import atomic_write


def _load_yaml_list(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return []
    if isinstance(data, dict):  # tolerate {items: [...]}
        data = data.get("items", [])
    if not isinstance(data, list):
        raise ValueError(f"{path} はリスト形式である必要があります")
    return data


def _dump_yaml_list(path: Path, items: list[dict]) -> None:
    header = f"# comdiary registry — {path.name}\n# 人が編集するファイルです。\n"
    body = yaml.safe_dump(items, allow_unicode=True, sort_keys=False, width=100)
    atomic_write(path, header + body)


@dataclass
class Registry:
    root: Path
    projects: list[Project]
    people: list[Person]
    glossary: dict[str, str]

    # -- loading ----------------------------------------------------------
    @classmethod
    def load(cls, ledger: Path) -> Registry:
        root = ledger / "registry"
        projects = [Project.model_validate(d) for d in _load_yaml_list(root / "projects.yaml")]
        people = [Person.model_validate(d) for d in _load_yaml_list(root / "people.yaml")]
        glossary: dict[str, str] = {}
        gpath = root / "glossary.yaml"
        if gpath.is_file():
            raw = yaml.safe_load(gpath.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                glossary = {str(k): str(v) for k, v in raw.items()}
        return cls(root=root, projects=projects, people=people, glossary=glossary)

    # -- lookup -----------------------------------------------------------
    def project(self, project_id: str) -> Project | None:
        return next((p for p in self.projects if p.id == project_id), None)

    def active_projects(self) -> list[Project]:
        return [p for p in self.projects if p.status == "active"]

    def person(self, person_id: str) -> Person | None:
        return next((p for p in self.people if p.id == person_id), None)

    def resolve_person(self, label: str) -> Person | None:
        """Map a free-text speaker label onto a registered person, if possible."""
        from ..util import normalize_match

        key = normalize_match(label)
        for person in self.people:
            candidates = [person.id, person.name, *person.aliases]
            if any(normalize_match(c) == key for c in candidates):
                return person
        return None

    def display_person(self, label: str | None) -> str:
        if not label:
            return "不明"
        person = self.resolve_person(label)
        return person.name if person else label

    # -- mutation ---------------------------------------------------------
    def add_project(self, project: Project) -> None:
        if self.project(project.id):
            raise ValueError(f"案件 id '{project.id}' は既に存在します")
        self.projects.append(project)
        self.save_projects()

    def save_projects(self) -> None:
        _dump_yaml_list(
            self.root / "projects.yaml",
            [p.model_dump(mode="json", exclude_none=True) for p in self.projects],
        )

    def save_people(self) -> None:
        _dump_yaml_list(
            self.root / "people.yaml",
            [p.model_dump(mode="json", exclude_none=True) for p in self.people],
        )

    # -- prompt material --------------------------------------------------
    def digest(self, include_closed: bool = False) -> str:
        """Compact catalogue handed to the LLM for project classification."""
        lines: list[str] = []
        for p in self.projects:
            if p.status == "closed" and not include_closed:
                continue
            bits = [f"- id: {p.id}", f"  name: {p.name}", f"  status: {p.status}"]
            if p.summary:
                bits.append(f"  summary: {p.summary}")
            if p.aliases:
                bits.append(f"  aliases: {', '.join(p.aliases)}")
            if p.keywords:
                bits.append(f"  keywords: {', '.join(p.keywords)}")
            if p.members:
                bits.append(f"  members: {', '.join(self.display_person(m) for m in p.members)}")
            lines.append("\n".join(bits))
        return "\n".join(lines) if lines else "(登録済みの案件はまだありません)"

    def people_digest(self) -> str:
        if not self.people:
            return "(登録済みの人物はまだありません)"
        return "\n".join(
            f"- id: {p.id} / name: {p.name}"
            + (f" / aliases: {', '.join(p.aliases)}" if p.aliases else "")
            + (f" / role: {p.role}" if p.role else "")
            for p in self.people
        )

    def glossary_digest(self) -> str:
        if not self.glossary:
            return ""
        return "\n".join(f"- {k}: {v}" for k, v in self.glossary.items())
