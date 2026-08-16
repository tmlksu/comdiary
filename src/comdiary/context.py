"""Context packs — the payload an LLM actually wants.

This is what makes the ledger useful for the real jobs: drafting an agenda,
preparing material that anticipates what people care about, or thinking through
"what do we do next" with the history at hand.

A pack is assembled by ordinary queries, not by a model, so it is cheap,
deterministic and auditable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .ingest.state import State
from .ledger.paths import LedgerPaths
from .models import Project
from .registry.store import Registry

PURPOSES: dict[str, str] = {
    "general": "この案件の現状把握",
    "agenda": "次回会議のアジェンダ作成",
    "material": "説明資料・提案資料の作成",
    "tasks": "タスク整理・次アクションの棚卸し",
    "handoff": "第三者への引き継ぎ",
}

PURPOSE_HINTS: dict[str, str] = {
    "agenda": (
        "未解決の論点と、前回持ち越しのアクションを起点に組み立ててください。"
        "温度感が高かった論点は、扱い方に注意が要ります。"
    ),
    "material": (
        "各人の関心事(concerns)に先回りして答える構成にしてください。"
        "決定済み事項は前提として簡潔に、未解決事項は選択肢の形で示すと通りやすいです。"
    ),
    "tasks": "オーナー未定・期限未定のアクションを優先的に洗い出してください。",
    "handoff": "経緯(なぜそう決まったか)と、まだ誰も引き取っていない事項を厚めに。",
}


@dataclass
class ContextPack:
    project: Project | None
    purpose: str
    generated_at: datetime
    summary: str = ""
    recent: list[dict[str, Any]] = field(default_factory=list)
    open_questions: list[dict[str, Any]] = field(default_factory=list)
    open_actions: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    concerns: list[dict[str, Any]] = field(default_factory=list)
    docs: list[str] = field(default_factory=list)
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project.model_dump(mode="json") if self.project else None,
            "purpose": self.purpose,
            "purpose_label": PURPOSES.get(self.purpose, self.purpose),
            "generated_at": self.generated_at.isoformat(timespec="seconds"),
            "summary": self.summary,
            "recent_segments": self.recent,
            "open_questions": self.open_questions,
            "open_actions": self.open_actions,
            "recent_decisions": self.decisions,
            "concerns": self.concerns,
            "ledger_docs": self.docs,
            "hint": self.hint,
        }

    def to_markdown(self) -> str:
        name = self.project.name if self.project else "(案件未指定)"
        out = [f"# コンテキストパック: {name}", ""]
        out.append(f"- 目的: {PURPOSES.get(self.purpose, self.purpose)}")
        out.append(f"- 生成: {self.generated_at:%Y-%m-%d %H:%M}")
        if self.project and self.project.summary:
            out.append(f"- 概要: {self.project.summary}")
        out.append("")

        def section(title: str, rows: list[str]) -> None:
            if not rows:
                return
            out.append(f"## {title}")
            out.extend(rows)
            out.append("")

        section(
            "直近の動き",
            [f"- {r['date'][:10]} **{r['title']}** — {r['summary']}" for r in self.recent],
        )
        section(
            "未解決の論点",
            [f"- {r['text']}" + (f" (提起: {r['owner']})" if r.get("owner") else "")
             for r in self.open_questions],
        )
        section(
            "未完了のアクション",
            [
                f"- {r['text']} — 担当: {r.get('owner') or '未定'} / 期限: {r.get('due') or '未定'}"
                for r in self.open_actions
            ],
        )
        section("直近の決定事項", [f"- {r['date'][:10]} {r['text']}" for r in self.decisions])
        section(
            "関係者が気にしていること",
            [
                f"- **{r['who']}** / {r['topic']} ({r['kind']}, {r['intensity']}): "
                f"{r.get('concern') or r.get('evidence') or ''}"
                for r in self.concerns
            ],
        )
        section("参照できる台帳ファイル", [f"- `{d}`" for d in self.docs])
        if self.hint:
            out += ["## 使い方のヒント", self.hint, ""]
        return "\n".join(out)


def build_pack(
    ledger: Path,
    project_id: str | None,
    purpose: str = "general",
    months: int = 6,
    limit: int = 12,
) -> ContextPack:
    registry = Registry.load(ledger)
    paths = LedgerPaths(ledger)
    project = registry.project(project_id) if project_id else None
    since = (datetime.now().astimezone() - timedelta(days=30 * months)).isoformat(timespec="seconds")

    with State(paths.state_db) as state:
        recent = [
            {
                "date": r["date"],
                "title": r["title"],
                "summary": r["summary"],
                "meeting": r["meeting_title"],
                "doc": r["meeting_doc"],
            }
            for r in (state.recent_segments(project_id, limit) if project_id else [])
        ]
        # `owner` holds a registry id (that is what the LLM is told to emit);
        # a context pack is read by a human or quoted into a document, so resolve.
        questions = [
            {
                "text": r["text"],
                "owner": registry.display_person(r["owner"]) if r["owner"] else None,
                "date": r["date"],
                "project": r["project_id"],
            }
            for r in state.open_items("question", project_id, limit=40)
        ]
        actions = [
            {
                "text": r["text"],
                "owner": registry.display_person(r["owner"]) if r["owner"] else None,
                "due": r["due"],
                "date": r["date"],
                "project": r["project_id"],
            }
            for r in state.open_items("action", project_id, limit=40)
        ]
        decisions = [
            {"text": r["text"], "date": r["date"], "project": r["project_id"]}
            for r in state.conn.execute(
                "SELECT * FROM items WHERE item_type='decision'"
                + (" AND project_id = ?" if project_id else "")
                + " AND date >= ? ORDER BY date DESC LIMIT ?",
                ([project_id] if project_id else []) + [since, 25],
            ).fetchall()
        ]
        concerns = [
            {
                "who": registry.display_person(r["speaker"]),
                "topic": r["topic"],
                "kind": r["kind"],
                "intensity": r["intensity"],
                "concern": r["concern"],
                "evidence": r["evidence"],
                "date": r["date"],
                "confidence": r["confidence"],
            }
            for r in state.concerns(project_id=project_id, since=since, limit=40)
        ]

    docs: list[str] = []
    if project_id:
        for path in (
            paths.project_readme(project_id),
            paths.project_open_questions(project_id),
            paths.project_handoff(project_id),
        ):
            if path.is_file():
                docs.append(str(path))
        adr_dir = paths.project_adr_dir(project_id)
        if adr_dir.is_dir():
            docs += [str(p) for p in sorted(adr_dir.glob("*.md"))]

    return ContextPack(
        project=project,
        purpose=purpose,
        generated_at=datetime.now().astimezone(),
        summary=project.summary if project else "",
        recent=recent,
        open_questions=questions,
        open_actions=actions,
        decisions=decisions,
        concerns=concerns,
        docs=docs,
        hint=PURPOSE_HINTS.get(purpose, ""),
    )


def aggregate_concerns(
    ledger: Path,
    speaker: str | None = None,
    project_id: str | None = None,
    months: int = 12,
) -> list[dict[str, Any]]:
    """Roll signals up per (person, topic) — recurrence is the interesting part."""
    registry = Registry.load(ledger)
    paths = LedgerPaths(ledger)
    since = (datetime.now().astimezone() - timedelta(days=30 * months)).isoformat(timespec="seconds")

    resolved = speaker
    if speaker:
        person = registry.resolve_person(speaker)
        resolved = person.id if person else speaker

    with State(paths.state_db) as state:
        rows = state.concerns(speaker=resolved, project_id=project_id, since=since, limit=500)

    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        who = registry.display_person(r["speaker"]) if r["speaker"] else "(話者不明)"
        key = (who, r["topic"])
        bucket = buckets.setdefault(
            key,
            {
                "who": who,
                "topic": r["topic"],
                "count": 0,
                "kinds": [],
                "max_intensity": "low",
                "concerns": [],
                "latest": "",
                "projects": [],
            },
        )
        bucket["count"] += 1
        if r["kind"] not in bucket["kinds"]:
            bucket["kinds"].append(r["kind"])
        order = {"low": 0, "medium": 1, "high": 2}
        if order[r["intensity"]] > order[bucket["max_intensity"]]:
            bucket["max_intensity"] = r["intensity"]
        if r["concern"] and r["concern"] not in bucket["concerns"]:
            bucket["concerns"].append(r["concern"])
        if r["project_id"] and r["project_id"] not in bucket["projects"]:
            bucket["projects"].append(r["project_id"])
        bucket["latest"] = max(bucket["latest"], r["date"])

    order = {"low": 0, "medium": 1, "high": 2}
    return sorted(
        buckets.values(),
        key=lambda b: (-b["count"], -order[b["max_intensity"]], b["latest"]),
    )
