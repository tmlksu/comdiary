"""LLM backends.

The only thing comdiary asks an LLM to do is return JSON matching a pydantic
model. Everything downstream — markdown, the ledger, the index — is produced by
ordinary code from that validated object.

Two shapes of backend live behind one Protocol:

* **subprocess** (GitHub Copilot CLI) — a chat tool that answers in prose, so
  the JSON has to be described in the prompt, scraped back out of the reply and
  re-tried when it does not validate.
* **HTTP API** (Gemini) — constrains decoding to the schema, so none of that is
  needed; the retry loop stays only as a fallback.

The transcript is passed as ``context`` rather than baked into ``prompt``.
Subprocess backends just concatenate the two, but a meeting is summarised once
and then queried once per segment, so an API backend can upload the transcript
a single time and reuse it — see `gemini.GeminiBackend`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from ..config import LLMConfig

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class LLMBackend(Protocol):
    name: str

    def complete_json(self, prompt: str, schema: type[T], context: str | None = None) -> T: ...

    def preflight(self) -> tuple[bool, str]:
        """Can this backend plausibly run? Local checks only, no network."""
        ...

    def probe(self) -> tuple[bool, str]:
        """One real round trip. Costs a call, so only `doctor --probe` uses it."""
        ...

    def release_context(self) -> None:
        """The current ``context`` is finished with — drop anything held for it."""
        ...

    def close(self) -> None: ...


_FENCE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def extract_json(raw: str) -> dict:
    """Pull a JSON object out of a chatty CLI response."""
    text = raw.strip()
    if not text:
        raise LLMError("LLM の応答が空でした")

    fenced = _FENCE.findall(text)
    for candidate in (*fenced, text):
        candidate = candidate.strip()
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            # Fall back to the outermost {...} span.
            start, end = candidate.find("{"), candidate.rfind("}")
            if start == -1 or end <= start:
                continue
            try:
                data = json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                continue
        if isinstance(data, dict):
            return data
    raise LLMError(f"応答から JSON を取り出せませんでした: {text[:400]}")


def schema_hint(schema: type[BaseModel]) -> str:
    return json.dumps(schema.model_json_schema(), ensure_ascii=False, indent=2)


def compose_prompt(prompt: str, context: str | None) -> str:
    """Fold the transcript back into the prompt, for backends that cannot
    carry it separately. The wording matches what the prompts used to build
    inline, so no backend sees a changed prompt because of this split."""
    if not context:
        return prompt
    return f"{prompt}\n\n## 議事録\n<transcript>\n{context}\n</transcript>\n"


FORMAT_BLOCK = (
    "\n\n## 出力形式\n"
    "以下の JSON Schema に厳密に従う JSON オブジェクトを**1つだけ**出力してください。\n"
    "説明文・前置き・後書きは一切不要です。JSON のみを出力してください。\n"
    "確信が持てない項目は推測で埋めず、省略するか null / 空配列にしてください。\n\n"
    "```json\n{schema}\n```\n"
)


class _RetryingBackend:
    """Shared validation/retry loop.

    Subclasses implement `_run`. A backend that can constrain decoding to the
    schema says so via `_native_schema`, which drops the schema dump from the
    prompt — that block is several kilobytes and would otherwise be billed on
    every single call.
    """

    name = "base"

    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg

    def _native_schema(self, schema: type[BaseModel]) -> object | None:
        """Return a transport-native schema, or None to describe it in prose."""
        return None

    def _run(
        self, prompt: str, context: str | None = None, schema: object | None = None
    ) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def preflight(self) -> tuple[bool, str]:
        return True, ""

    def release_context(self) -> None:
        return None

    def close(self) -> None:
        self.release_context()

    def probe(self) -> tuple[bool, str]:
        """One cheap round trip, used by `comdiary doctor`."""
        try:
            self._run("Reply with exactly this and nothing else: COMDIARY_OK")
        except LLMError as exc:
            return False, str(exc)
        return True, f"model={self.cfg.model or 'auto'}"

    def complete_json(self, prompt: str, schema: type[T], context: str | None = None) -> T:
        native = self._native_schema(schema)
        base = prompt if native else prompt + FORMAT_BLOCK.format(schema=schema_hint(schema))
        attempt_prompt = base
        last_error = ""
        for attempt in range(self.cfg.retries + 1):
            raw = self._run(attempt_prompt, context, native)
            try:
                return schema.model_validate(extract_json(raw))
            except (LLMError, ValidationError) as exc:
                last_error = str(exc)
                attempt_prompt = (
                    f"{base}\n\n"
                    f"## 直前の試行はスキーマ検証に失敗しました (試行 {attempt + 1})\n"
                    f"エラー:\n{last_error[:1500]}\n"
                    "同じ誤りを繰り返さず、スキーマに適合する JSON のみを出力してください。"
                )
        raise LLMError(
            f"{self.cfg.retries + 1} 回試行しましたがスキーマ検証に失敗しました: {last_error[:800]}"
        )


class CopilotBackend(_RetryingBackend):
    """GitHub Copilot CLI in non-interactive mode.

    The prompt goes in on **stdin**, not via ``-p``. A meeting transcript plus
    the JSON schema routinely exceeds Windows' 32767-character command-line
    limit, and Windows is a first-class target here (Google Drive's stream mount
    only exists there), so passing it as an argument would fail outright.

    Copilot writes the answer to stdout and its usage footer to stderr, so
    stdout can be parsed directly.
    """

    name = "copilot"

    def _argv(self) -> list[str]:
        argv = [
            self.cfg.command,
            "--allow-all-tools",  # required for non-interactive mode
            "--no-color",
            "--log-level",
            "none",
        ]
        if self.cfg.model and self.cfg.model != "auto":
            argv += ["--model", self.cfg.model]
        argv += list(self.cfg.extra_args)
        return argv

    def preflight(self) -> tuple[bool, str]:
        found = shutil.which(self.cfg.command)
        if found:
            return True, found
        return False, (
            f"'{self.cfg.command}' が見つかりません。"
            "npm install -g @github/copilot で導入してください"
        )

    def _run(
        self, prompt: str, context: str | None = None, schema: object | None = None
    ) -> str:
        ok, detail = self.preflight()
        if not ok:
            raise LLMError(f"{detail}。`comdiary doctor` で確認してください。")
        try:
            proc = subprocess.run(
                self._argv(),
                input=compose_prompt(prompt, context),
                capture_output=True,
                text=True,
                timeout=self.cfg.timeout,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"LLM 呼び出しが {self.cfg.timeout}s でタイムアウトしました") from exc
        if proc.returncode != 0:
            raise LLMError(
                f"{self.cfg.command} が終了コード {proc.returncode} で失敗しました: "
                f"{(proc.stderr or proc.stdout or '')[:600]}"
            )
        out = proc.stdout.strip()
        if not out:
            # A bad --model name is reported on stderr with a zero exit code.
            raise LLMError(f"{self.cfg.command} が空応答を返しました: {proc.stderr.strip()[:400]}")
        return out


class NullBackend:
    name = "none"

    def complete_json(self, prompt: str, schema: type[T], context: str | None = None) -> T:
        raise LLMError(
            "LLM バックエンドが 'none' です。設定ファイルの [llm] backend を指定してください"
            " (場所は `comdiary config` で確認できます)。"
        )

    def preflight(self) -> tuple[bool, str]:
        return True, "呼び出しを行いません"

    def probe(self) -> tuple[bool, str]:
        return True, "呼び出しを行いません"

    def release_context(self) -> None:
        return None

    def close(self) -> None:
        return None


def build_backend(cfg: LLMConfig) -> LLMBackend:
    if cfg.backend == "copilot":
        return CopilotBackend(cfg)
    if cfg.backend == "gemini":
        from .gemini import GeminiBackend

        return GeminiBackend(cfg)
    if cfg.backend == "none":
        return NullBackend()
    if cfg.backend == "fake":
        from .fake import FakeBackend

        return FakeBackend(cfg)
    raise LLMError(f"未知の LLM バックエンド: {cfg.backend}")
