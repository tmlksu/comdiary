"""Gemini backend: schema translation, context caching, error surfacing.

Nothing here touches the network — `_request` (or `urlopen` below it) is
stubbed. The point of most of these is the *billing* shape: how many times the
transcript crosses the wire, and whether the multi-kilobyte schema dump is still
riding along in the prompt.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest
from pydantic import BaseModel

from comdiary.config import Config, GeminiConfig, LLMConfig
from comdiary.ingest.pipeline import Pipeline
from comdiary.ingest.state import State
from comdiary.ledger.paths import LedgerPaths
from comdiary.llm.backend import LLMError, build_backend
from comdiary.llm.gemini import DEFAULT_MODEL, GeminiBackend
from comdiary.llm.schema import SchemaUnsupported, to_gemini_schema
from comdiary.models import DetailResponse, SpeakerStats, SplitResponse

from .conftest import SAMPLE_LUNCH


def cfg(**gemini) -> LLMConfig:
    return LLMConfig(backend="gemini", model="gemini-test", gemini=GeminiConfig(**gemini))


# ---------------------------------------------------------------------------
# schema translation
# ---------------------------------------------------------------------------


class TestSchema:
    @pytest.mark.parametrize("model", [SplitResponse, DetailResponse])
    def test_no_json_schema_only_constructs_survive(self, model):
        """$ref/$defs are the whole reason this module exists — Gemini's dialect
        has no way to express them."""
        text = json.dumps(to_gemini_schema(model))
        assert "$ref" not in text and "$defs" not in text and "anyOf" not in text

    def test_optional_field_becomes_nullable(self):
        owner = to_gemini_schema(DetailResponse)["properties"]["actions"]["items"]
        assert owner["properties"]["owner"] == {"type": "STRING", "nullable": True}

    def test_literal_becomes_enum(self):
        status = to_gemini_schema(DetailResponse)["properties"]["actions"]["items"][
            "properties"
        ]["status"]
        assert status["type"] == "STRING"
        assert set(status["enum"]) == {"open", "done", "dropped"}

    def test_property_ordering_is_declared(self):
        schema = to_gemini_schema(DetailResponse)
        assert schema["propertyOrdering"][:2] == ["summary", "topics"]

    def test_descriptions_are_kept(self):
        topics = to_gemini_schema(DetailResponse)["properties"]["topics"]
        assert "短い名詞句" in topics["description"]

    def test_open_ended_dict_is_refused_not_silently_flattened(self):
        """SpeakerStats has dict[str, float]. Emitting `{}` for it would let the
        model return a shape pydantic then rejects, and the failure would look
        like the model's fault."""
        with pytest.raises(SchemaUnsupported):
            to_gemini_schema(SpeakerStats)

    def test_recursion_is_refused(self):
        class Node(BaseModel):
            child: Node | None = None

        Node.model_rebuild()
        with pytest.raises(SchemaUnsupported):
            to_gemini_schema(Node)


# ---------------------------------------------------------------------------
# request shaping
# ---------------------------------------------------------------------------


class Recorder(GeminiBackend):
    """A backend whose transport is a list instead of a socket."""

    def __init__(self, config: LLMConfig | None = None, cache_name: str | None = "cachedContents/c1"):
        super().__init__(config or cfg(cache_min_chars=1))
        self.requests: list[tuple[str, str, dict | None]] = []
        self.cache_name = cache_name

    def _request(self, method, path, payload=None):
        self.requests.append((method, path, payload))
        if path == "cachedContents":
            if self.cache_name is None:
                raise LLMError("HTTP 400: cached content is too small")
            return {"name": self.cache_name} if self.cache_name else {}
        if method == "DELETE":
            return {}
        return {"candidates": [{"content": {"parts": [{"text": "{}"}]}, "finishReason": "STOP"}]}

    def generates(self) -> list[dict]:
        return [p for m, path, p in self.requests if "generateContent" in path]


class TestRequestShape:
    def test_schema_is_sent_natively_and_dropped_from_the_prompt(self):
        backend = Recorder()
        backend.complete_json("抽出してください", DetailResponse, context="本文" * 50)
        payload = backend.generates()[0]
        assert payload["generationConfig"]["responseMimeType"] == "application/json"
        assert payload["generationConfig"]["responseSchema"]["type"] == "OBJECT"
        # The prose schema dump is several kB and would be billed on every call.
        sent = json.dumps(payload["contents"], ensure_ascii=False)
        assert "## 出力形式" not in sent and "JSON Schema" not in sent

    def test_unconvertible_schema_falls_back_to_describing_it_in_prose(self):
        backend = Recorder()
        backend.complete_json("抽出してください", SpeakerStats, context="本文" * 50)
        payload = backend.generates()[0]
        assert "responseSchema" not in payload["generationConfig"]
        assert "## 出力形式" in json.dumps(payload["contents"], ensure_ascii=False)

    def test_transcript_is_uploaded_once_and_then_referenced(self):
        backend = Recorder()
        for _ in range(3):
            backend.complete_json("抽出", DetailResponse, context="議事録本文")
        creates = [r for r in backend.requests if r[1] == "cachedContents"]
        assert len(creates) == 1
        for payload in backend.generates():
            assert payload["cachedContent"] == "cachedContents/c1"
            assert "議事録本文" not in json.dumps(payload["contents"], ensure_ascii=False)

    def test_a_new_document_replaces_the_previous_cache(self):
        backend = Recorder()
        backend.complete_json("抽出", DetailResponse, context="ドキュメントA")
        backend.complete_json("抽出", DetailResponse, context="ドキュメントB")
        assert [r[0] for r in backend.requests if r[0] == "DELETE"] == ["DELETE"]
        assert len([r for r in backend.requests if r[1] == "cachedContents"]) == 2

    def test_release_context_deletes_the_cache_rather_than_waiting_for_ttl(self):
        backend = Recorder()
        backend.complete_json("抽出", DetailResponse, context="本文")
        backend.release_context()
        assert ("DELETE", "cachedContents/c1", None) in backend.requests
        # ...and is idempotent, so `close()` after it is not a second call.
        backend.release_context()
        assert len([r for r in backend.requests if r[0] == "DELETE"]) == 1

    def test_a_nameless_cache_response_is_treated_as_a_refusal(self):
        backend = Recorder()
        backend.cache_name = ""
        for _ in range(3):
            backend.complete_json("抽出", DetailResponse, context="本文" * 50)
        assert len([r for r in backend.requests if r[1] == "cachedContents"]) == 1

    def test_a_refused_cache_falls_back_inline_and_is_not_retried(self):
        """Caching is an optimisation. Losing it must not lose the ingest, and
        must not cost a failed create call per segment either."""
        backend = Recorder(cache_name=None)
        for _ in range(3):
            backend.complete_json("抽出", DetailResponse, context="短い本文")
        assert len([r for r in backend.requests if r[1] == "cachedContents"]) == 1
        for payload in backend.generates():
            assert "cachedContent" not in payload
            assert "短い本文" in json.dumps(payload["contents"], ensure_ascii=False)

    def test_short_transcripts_skip_the_cache_entirely(self):
        backend = Recorder(cfg(cache_min_chars=10_000))
        backend.complete_json("抽出", DetailResponse, context="短い")
        assert not [r for r in backend.requests if r[1] == "cachedContents"]

    def test_caching_can_be_turned_off(self):
        backend = Recorder(cfg(cache=False))
        backend.complete_json("抽出", DetailResponse, context="本文" * 50)
        assert not [r for r in backend.requests if r[1] == "cachedContents"]

    def test_cached_and_uncached_calls_order_the_prompt_the_same_way(self):
        """A cache miss must change what a call costs, never what it answers."""
        cached, plain = Recorder(), Recorder(cfg(cache=False))
        for backend in (cached, plain):
            backend.complete_json("抽出せよ", DetailResponse, context="議事録本文")
        plain_texts = [p["text"] for c in plain.generates()[0]["contents"] for p in c["parts"]]
        assert plain_texts[0].startswith("## 議事録")
        assert plain_texts[1] == "抽出せよ"
        assert [p["text"] for c in cached.generates()[0]["contents"] for p in c["parts"]] == [
            "抽出せよ"
        ]

    def test_thinking_budget_is_only_sent_when_configured(self):
        assert "thinkingConfig" not in Recorder()._generation_config(None)
        backend = Recorder(cfg(thinking_budget=0))
        assert backend._generation_config(None)["thinkingConfig"] == {"thinkingBudget": 0}

    def test_model_auto_resolves_to_a_real_name(self):
        assert GeminiBackend(LLMConfig(backend="gemini", model="auto")).model == DEFAULT_MODEL
        assert GeminiBackend(LLMConfig(backend="gemini", model="gemini-x")).model == "gemini-x"


# ---------------------------------------------------------------------------
# responses and failures
# ---------------------------------------------------------------------------


class TestResponses:
    def test_truncated_output_is_an_error_not_a_silent_half_record(self):
        with pytest.raises(LLMError, match="max_output_tokens"):
            GeminiBackend._text(
                {"candidates": [{"content": {"parts": [{"text": "{}"}]}, "finishReason": "MAX_TOKENS"}]}
            )

    def test_empty_output_explains_the_thinking_budget(self):
        with pytest.raises(LLMError, match="thinking_budget"):
            GeminiBackend._text({"candidates": [{"finishReason": "MAX_TOKENS"}]})

    def test_blocked_prompt_says_so(self):
        with pytest.raises(LLMError, match="blockReason=SAFETY"):
            GeminiBackend._text({"promptFeedback": {"blockReason": "SAFETY"}})


class TestTransport:
    """The one layer that does touch urllib."""

    def _urlopen(self, monkeypatch, responses):
        seen = []

        def fake(req, timeout=None):
            seen.append(req)
            item = responses[min(len(seen) - 1, len(responses) - 1)]
            if isinstance(item, int):
                raise urllib.error.HTTPError(
                    req.full_url, item, "boom", {}, io.BytesIO(b'{"error":"boom"}')
                )
            return io.BytesIO(json.dumps(item).encode())

        monkeypatch.setattr(urllib.request, "urlopen", fake)
        monkeypatch.setattr("comdiary.llm.gemini.time.sleep", lambda _s: None)
        return seen

    def test_the_api_key_never_lands_in_the_url(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "secret-key")
        seen = self._urlopen(monkeypatch, [{"ok": True}])
        GeminiBackend(cfg())._request("POST", "models/x:generateContent", {})
        assert "secret-key" not in seen[0].full_url
        assert seen[0].get_header("X-goog-api-key") == "secret-key"

    def test_a_bad_request_is_not_retried(self, monkeypatch):
        """A 400 is our bug, not the server's. Retrying bills it three times."""
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        seen = self._urlopen(monkeypatch, [400])
        with pytest.raises(LLMError, match="HTTP 400"):
            GeminiBackend(cfg())._request("POST", "models/x:generateContent", {})
        assert len(seen) == 1

    def test_a_transient_failure_is_retried(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        seen = self._urlopen(monkeypatch, [503, 503, {"ok": True}])
        assert GeminiBackend(cfg())._request("POST", "models/x:generateContent", {}) == {"ok": True}
        assert len(seen) == 3

    def test_a_missing_key_is_caught_before_any_call(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        backend = build_backend(cfg())
        ok, detail = backend.preflight()
        assert not ok and "GEMINI_API_KEY" in detail
        with pytest.raises(LLMError, match="GEMINI_API_KEY"):
            backend.complete_json("x", DetailResponse)

    def test_the_key_env_var_name_is_configurable(self, monkeypatch):
        monkeypatch.setenv("WORK_GEMINI_KEY", "k")
        ok, _ = build_backend(cfg(api_key_env="WORK_GEMINI_KEY")).preflight()
        assert ok


# ---------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------


class PipelineRecorder(Recorder):
    """Answers generateContent with a shape matching whichever pass asked."""

    def _request(self, method, path, payload=None):
        if "generateContent" not in path:
            return super()._request(method, path, payload)
        self.requests.append((method, path, payload))
        wanted = payload["generationConfig"]["responseSchema"]["properties"]
        if "segments" in wanted:
            body = SplitResponse(
                title="昼会",
                segments=[
                    {"segment_id": "s1", "title": "基盤移行"},
                    {"segment_id": "s2", "title": "採用サイト"},
                ],
            )
        else:
            body = DetailResponse(summary="要約")
        return {
            "candidates": [
                {"content": {"parts": [{"text": body.model_dump_json()}]}, "finishReason": "STOP"}
            ]
        }


def test_a_meeting_uploads_its_transcript_once_not_once_per_segment(config: Config, tmp_path):
    """The reason caching is on by default: without it a 2-segment meeting bills
    the transcript three times."""
    (config.ingest.inbox / "昼会.md").write_text(SAMPLE_LUNCH, encoding="utf-8")
    backend = PipelineRecorder(cfg(cache_min_chars=1))

    with State(LedgerPaths(config.ledger).state_db) as state:
        report = Pipeline(config, backend, state).run()

    assert report.processed == 1
    assert report.outcomes[0].llm_calls == 3  # one split + two details
    assert len([r for r in backend.requests if r[1] == "cachedContents"]) == 1
    for payload in backend.generates():
        assert payload["cachedContent"] == "cachedContents/c1"
        # A verbatim transcript line — the registry digest also names people.
        assert "予備日を1週間取る" not in json.dumps(payload["contents"], ensure_ascii=False)
    # The document is done; holding the cache past that is billed for nothing.
    assert ("DELETE", "cachedContents/c1", None) in backend.requests
