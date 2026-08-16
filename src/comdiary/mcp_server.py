"""A deliberately thin MCP server.

It exposes the *read* side of the ledger plus one guarded write, so an LLM can
use the ledger as retrieval for the jobs this was built for: drafting agendas,
preparing material that anticipates people's concerns, and thinking through
next steps.

Thin on purpose — every tool here is a direct call into the same functions the
CLI uses, so there is one behaviour to reason about, not two.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .config import Config, load_config
from .context import PURPOSES, aggregate_concerns, build_pack
from .ingest.state import State
from .ledger.paths import LedgerPaths
from .models import Meeting
from .registry.store import Registry
from .util import atomic_write, now


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _server_class():
    """The SDK renamed FastMCP -> MCPServer in 2.0; both expose .tool()/.run()."""
    try:
        from mcp.server.mcpserver import MCPServer  # mcp >= 2.0

        return MCPServer
    except ImportError:
        pass
    try:
        from mcp.server.fastmcp import FastMCP  # mcp 1.x

        return FastMCP
    except ImportError as exc:
        raise SystemExit(
            "MCP サーバには追加依存が必要です: uv sync --extra mcp"
        ) from exc


def build_server(cfg: Config):
    mcp = _server_class()(name="comdiary")
    paths = LedgerPaths(cfg.ledger)

    @mcp.tool()
    def project_list(include_closed: bool = False) -> str:
        """登録されている案件の一覧を返す。まずこれを呼んで案件 id を確認する。"""
        registry = Registry.load(cfg.ledger)
        items = [
            p.model_dump(mode="json")
            for p in registry.projects
            if include_closed or p.status != "closed"
        ]
        return _json(items)

    @mcp.tool()
    def context_pack(project_id: str, purpose: str = "general") -> str:
        """案件の現状・未解決論点・未完了アクション・関係者の関心事をまとめて返す。

        資料作成・アジェンダ作成・タスク整理・壁打ちの起点として最初に呼ぶべきツール。
        purpose は general / agenda / material / tasks / handoff のいずれか。
        """
        if purpose not in PURPOSES:
            purpose = "general"
        return _json(build_pack(cfg.ledger, project_id, purpose).to_dict())

    @mcp.tool()
    def ledger_search(query: str, project_id: str = "", since: str = "", limit: int = 20) -> str:
        """台帳を全文検索する。since は YYYY-MM-DD。"""
        with State(paths.state_db) as state:
            rows = state.search(query, project_id or None, since or None, limit)
            return _json([dict(r) for r in rows])

    @mcp.tool()
    def meeting_get(meeting_id: str) -> str:
        """会議の正本(全セグメント・温度感つき)を JSON で返す。"""
        for path in paths.meetings.rglob("*.json"):
            text = path.read_text(encoding="utf-8")
            if f'"meeting_id": "{meeting_id}"' in text[:400] or meeting_id in path.stem:
                return _json(Meeting.model_validate_json(text).model_dump(mode="json"))
        return _json({"error": f"会議 '{meeting_id}' が見つかりません"})

    @mcp.tool()
    def open_questions(project_id: str = "") -> str:
        """未解決の論点を返す。案件を跨いで見たいときは project_id を空に。"""
        with State(paths.state_db) as state:
            return _json([dict(r) for r in state.open_items("question", project_id or None)])

    @mcp.tool()
    def open_actions(project_id: str = "") -> str:
        """未完了のアクション(担当・期限つき)を返す。"""
        with State(paths.state_db) as state:
            return _json([dict(r) for r in state.open_items("action", project_id or None)])

    @mcp.tool()
    def person_concerns(person: str = "", project_id: str = "", months: int = 12) -> str:
        """誰が何を繰り返し気にしているかを集計して返す。

        資料を作るとき、相手の関心に先回りするために使う。
        話者帰属が不確実な記録(共通マイク等)は「(話者不明)」に集約される。
        """
        return _json(
            aggregate_concerns(cfg.ledger, person or None, project_id or None, months)
        )

    @mcp.tool()
    def timeline(project_id: str, limit: int = 20) -> str:
        """案件の直近の動きを時系列で返す。"""
        with State(paths.state_db) as state:
            return _json([dict(r) for r in state.recent_segments(project_id, limit)])

    @mcp.tool()
    def append_note(project_id: str, text: str, section: str = "logs") -> str:
        """台帳に追記する。section は logs / open-questions / handoff。

        これが唯一の書き込みツール。既存の記述は変更せず、末尾に追記する。
        """
        if section not in {"logs", "open-questions", "handoff"}:
            return _json({"error": f"section '{section}' には書き込めません"})
        registry = Registry.load(cfg.ledger)
        if not registry.project(project_id):
            return _json({"error": f"案件 '{project_id}' が見つかりません"})
        target = paths.section_path(project_id, section, datetime.now())
        stamp = now()
        entry = f"\n### {stamp:%Y-%m-%d %H:%M} (MCP 経由の追記)\n\n{text.strip()}\n"
        existing = target.read_text(encoding="utf-8") if target.is_file() else ""
        atomic_write(target, (existing.rstrip("\n") + "\n" + entry) if existing else entry.lstrip())
        return _json({"ok": True, "path": str(target)})

    return mcp


def serve(cfg: Config | None = None) -> None:  # pragma: no cover
    build_server(cfg or load_config()).run()


if __name__ == "__main__":  # pragma: no cover
    serve()
