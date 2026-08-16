from __future__ import annotations

import asyncio
import json

import pytest

from comdiary.config import Config
from comdiary.ingest.pipeline import Pipeline
from comdiary.ingest.state import State
from comdiary.ledger.paths import LedgerPaths
from comdiary.llm.fake import FakeBackend

from .conftest import SAMPLE_LUNCH

# comdiary.mcp_server imports fine without the extra — it only fails when a
# server is built. Guard on the SDK itself so the suite skips cleanly.
pytest.importorskip("mcp", reason="MCP の追加依存が未インストール (uv sync --extra mcp)")

from comdiary import mcp_server  # noqa: E402

EXPECTED_TOOLS = {
    "project_list",
    "context_pack",
    "ledger_search",
    "meeting_get",
    "open_questions",
    "open_actions",
    "person_concerns",
    "timeline",
    "append_note",
}


def _text(result) -> str:
    """Unwrap a tool result across SDK versions."""
    content = result.content if hasattr(result, "content") else result[0]
    if isinstance(content, list):
        content = content[0]
    return content.text


@pytest.fixture
def server(config: Config):
    (config.ingest.inbox / "2026-08-13-1200-昼会.md").write_text(SAMPLE_LUNCH, encoding="utf-8")
    with State(LedgerPaths(config.ledger).state_db) as state:
        Pipeline(config, FakeBackend(), state).run()
    return mcp_server.build_server(config)


def test_exposes_the_documented_tool_set(server):
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == EXPECTED_TOOLS


def test_every_tool_is_described_for_an_llm(server):
    for tool in asyncio.run(server.list_tools()):
        assert tool.description and len(tool.description) > 10, tool.name


def test_project_list_returns_json(server):
    payload = json.loads(_text(asyncio.run(server.call_tool("project_list", {}))))
    assert {p["id"] for p in payload} == {"alpha-migration", "recruit-site"}


def test_context_pack_returns_the_expected_keys(server):
    payload = json.loads(
        _text(asyncio.run(server.call_tool("context_pack", {"project_id": "alpha-migration"})))
    )
    assert payload["project"]["id"] == "alpha-migration"
    assert {"open_questions", "open_actions", "concerns", "recent_segments"} <= payload.keys()


def test_append_note_rejects_unknown_project(server):
    payload = json.loads(
        _text(asyncio.run(server.call_tool("append_note", {"project_id": "nope", "text": "x"})))
    )
    assert "error" in payload


def test_append_note_rejects_protected_sections(server):
    payload = json.loads(
        _text(
            asyncio.run(
                server.call_tool(
                    "append_note",
                    {"project_id": "alpha-migration", "text": "x", "section": "readme"},
                )
            )
        )
    )
    assert "error" in payload


def test_append_note_writes(server, config: Config):
    payload = json.loads(
        _text(
            asyncio.run(
                server.call_tool(
                    "append_note", {"project_id": "alpha-migration", "text": "MCP からのメモ"}
                )
            )
        )
    )
    assert payload["ok"]
    assert "MCP からのメモ" in open(payload["path"], encoding="utf-8").read()
