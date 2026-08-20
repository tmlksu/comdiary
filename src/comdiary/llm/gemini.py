"""Gemini (AI Studio) backend.

Written against the REST endpoint with `urllib` rather than the `google-genai`
SDK on purpose: pyproject keeps dependencies light so this runs on a low-powered
always-on arm64 box, and the surface actually used here is two endpoints. If
this ever needs Vertex AI's service-account auth, that is the point to take the
SDK on — not before.

Two things make this cheaper than the same prompts through a chat CLI:

* **Constrained decoding.** ``responseSchema`` makes the model emit the pydantic
  shape directly, so the multi-kilobyte JSON Schema dump disappears from every
  prompt and schema-validation retries stop happening. See `llm/schema.py` for
  the dialect translation, which is the only fiddly part.
* **Context caching.** A meeting costs one `split` call plus one `detail` call
  per segment, and every one of them needs the whole transcript. Uploading it
  once and referencing the cache turns "transcript × (1 + segments)" of billed
  input into roughly "transcript × 1". This is the single biggest cost lever in
  the pipeline, which is why it is on by default.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

from pydantic import BaseModel

from ..config import LLMConfig
from .backend import LLMError, _RetryingBackend
from .schema import SchemaUnsupported, to_gemini_schema

#: `model = "auto"` is a Copilot idiom — Gemini needs a real name.
DEFAULT_MODEL = "gemini-2.5-flash"

#: Transient upstream conditions. Anything else is a real error and must not be
#: retried: a 400 from a bad schema would otherwise be billed three times.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_RETRY_SLEEP = (2, 8)


def _transcript_block(context: str) -> str:
    return f"## 議事録\n<transcript>\n{context}\n</transcript>\n"


class GeminiBackend(_RetryingBackend):
    name = "gemini"

    def __init__(self, cfg: LLMConfig) -> None:
        super().__init__(cfg)
        self.opts = cfg.gemini
        self.model = cfg.model if cfg.model not in ("", "auto") else DEFAULT_MODEL
        self._schemas: dict[type[BaseModel], dict | None] = {}
        self._cache_name: str | None = None
        self._cache_key: str | None = None
        #: Set when the API refuses to cache (typically a transcript below the
        #: model's minimum). Retrying per document would burn a call each time.
        self._cache_off = False
        atexit.register(self.release_context)

    # -- config -----------------------------------------------------------
    def _api_key(self) -> str:
        key = os.environ.get(self.opts.api_key_env, "").strip()
        if not key:
            raise LLMError(
                f"環境変数 {self.opts.api_key_env} に Gemini の API キーが設定されていません。"
                "設定ファイルにキーそのものを書かないでください "
                "([llm.gemini] api_key_env で変数名だけ指定します)。"
            )
        return key

    def preflight(self) -> tuple[bool, str]:
        if os.environ.get(self.opts.api_key_env, "").strip():
            return True, f"{self.opts.api_key_env} 設定済み / model={self.model}"
        return False, f"環境変数 {self.opts.api_key_env} が未設定です"

    def probe(self) -> tuple[bool, str]:
        try:
            self._run("Reply with exactly this and nothing else: COMDIARY_OK")
        except LLMError as exc:
            return False, str(exc)
        return True, f"model={self.model}"

    # -- HTTP -------------------------------------------------------------
    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = f"{self.opts.base_url.rstrip('/')}/{path.lstrip('/')}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        last = ""
        for attempt in range(len(_RETRY_SLEEP) + 1):
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Content-Type", "application/json")
            # The key goes in a header, not the query string, so it stays out of
            # proxy logs and out of any traceback that prints the URL.
            req.add_header("x-goog-api-key", self._api_key())
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
                    body = resp.read().decode("utf-8")
                return json.loads(body) if body.strip() else {}
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:600]
                last = f"HTTP {exc.code}: {detail}"
                if exc.code not in _RETRY_STATUS or attempt >= len(_RETRY_SLEEP):
                    raise LLMError(f"Gemini API が失敗しました — {last}") from exc
            except urllib.error.URLError as exc:
                last = str(exc.reason)
                if attempt >= len(_RETRY_SLEEP):
                    raise LLMError(f"Gemini API に到達できません — {last}") from exc
            except TimeoutError as exc:
                last = f"{self.cfg.timeout}s でタイムアウト"
                if attempt >= len(_RETRY_SLEEP):
                    raise LLMError(f"Gemini API 呼び出しが {last}") from exc
            time.sleep(_RETRY_SLEEP[attempt])
        raise LLMError(f"Gemini API が失敗しました — {last}")

    # -- context cache ----------------------------------------------------
    def _cached_content(self, context: str) -> str | None:
        """Cache name for this transcript, creating it on first sight.

        Created eagerly rather than on the second use: a document always costs
        at least two calls (one split, one detail), so the write pays for itself
        immediately, and waiting would mean one extra full-price transcript.
        """
        if not self.opts.cache or self._cache_off:
            return None
        if len(context) < self.opts.cache_min_chars:
            # Below the model's minimum cacheable size the create call just
            # fails; skipping it keeps the failure out of the logs.
            return None

        key = hashlib.sha256(context.encode("utf-8")).hexdigest()
        if key == self._cache_key and self._cache_name:
            return self._cache_name
        self.release_context()  # a new document — the previous cache is dead weight

        try:
            created = self._request(
                "POST",
                "cachedContents",
                {
                    "model": f"models/{self.model}",
                    "contents": [
                        {"role": "user", "parts": [{"text": _transcript_block(context)}]}
                    ],
                    "ttl": f"{self.opts.cache_ttl_seconds}s",
                },
            )
        except LLMError:
            # Caching is an optimisation. Losing it must not lose the ingest —
            # every call simply carries the transcript inline from here on.
            self._cache_off = True
            return None
        self._cache_name = created.get("name")
        if not self._cache_name:
            # A 200 with no name is not something to keep asking about.
            self._cache_off = True
            return None
        self._cache_key = key
        return self._cache_name

    def release_context(self) -> None:
        """Drop the cache rather than waiting out its TTL — it is billed for
        as long as it lives."""
        name, self._cache_name, self._cache_key = self._cache_name, None, None
        if not name:
            return
        try:
            self._request("DELETE", name)
        except Exception:  # noqa: BLE001 - also runs from atexit, mid-teardown
            pass  # it expires on its own; nothing here is worth failing a run for

    # -- generation -------------------------------------------------------
    def _native_schema(self, schema: type[BaseModel]) -> dict | None:
        if schema not in self._schemas:
            try:
                self._schemas[schema] = to_gemini_schema(schema)
            except SchemaUnsupported:
                # Fall back to describing the schema in the prompt and letting
                # the inherited retry loop police the result.
                self._schemas[schema] = None
        return self._schemas[schema]

    def _generation_config(self, schema: dict | None) -> dict:
        config: dict = {"maxOutputTokens": self.opts.max_output_tokens}
        if self.opts.temperature is not None:
            config["temperature"] = self.opts.temperature
        if self.opts.thinking_budget is not None:
            config["thinkingConfig"] = {"thinkingBudget": self.opts.thinking_budget}
        if schema is not None:
            config["responseMimeType"] = "application/json"
            config["responseSchema"] = schema
        return config

    def _run(
        self, prompt: str, context: str | None = None, schema: object | None = None
    ) -> str:
        payload: dict = {"generationConfig": self._generation_config(schema)}  # type: ignore[arg-type]

        cached = self._cached_content(context) if context else None
        if cached:
            payload["cachedContent"] = cached
            contents = [{"role": "user", "parts": [{"text": prompt}]}]
        elif context:
            # Same order as the cached path, so a cache miss cannot change the
            # answer — only what it costs.
            contents = [
                {"role": "user", "parts": [{"text": _transcript_block(context)}]},
                {"role": "user", "parts": [{"text": prompt}]},
            ]
        else:
            contents = [{"role": "user", "parts": [{"text": prompt}]}]
        payload["contents"] = contents

        data = self._request("POST", f"models/{self.model}:generateContent", payload)
        return self._text(data)

    @staticmethod
    def _text(data: dict) -> str:
        if block := (data.get("promptFeedback") or {}).get("blockReason"):
            raise LLMError(f"Gemini がプロンプトを拒否しました (blockReason={block})")
        candidates = data.get("candidates") or []
        if not candidates:
            raise LLMError(f"Gemini の応答に candidates がありません: {str(data)[:400]}")
        candidate = candidates[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
        reason = candidate.get("finishReason")
        if not text:
            # MAX_TOKENS with an empty body is the classic "thinking ate the
            # whole budget" failure, and the message needs to say so.
            hint = {
                "MAX_TOKENS": "maxOutputTokens を増やすか thinking_budget を下げてください",
                "SAFETY": "安全性フィルタで停止しました",
                "RECITATION": "引用フィルタで停止しました",
            }.get(reason or "", "")
            raise LLMError(
                f"Gemini が空応答を返しました (finishReason={reason})"
                + (f" — {hint}" if hint else "")
            )
        if reason == "MAX_TOKENS":
            raise LLMError(
                "Gemini の応答が maxOutputTokens で打ち切られました。"
                "[llm.gemini] max_output_tokens を増やしてください。"
            )
        return text
