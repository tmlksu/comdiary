"""LLM backends.

The only thing comdiary asks an LLM to do is return JSON matching a pydantic
model. Everything downstream — markdown, the ledger, the index — is produced by
ordinary code from that validated object.

The default backend shells out to GitHub Copilot CLI. The Protocol exists so
this can be swapped for a local model later without touching the pipeline.
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

    def complete_json(self, prompt: str, schema: type[T]) -> T: ...


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


class _RetryingBackend:
    """Shared retry/validation loop for subprocess-based backends."""

    name = "base"

    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg

    def _run(self, prompt: str) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def complete_json(self, prompt: str, schema: type[T]) -> T:
        instruction = (
            f"{prompt}\n\n"
            "## 出力形式\n"
            "以下の JSON Schema に厳密に従う JSON オブジェクトを**1つだけ**出力してください。\n"
            "説明文・前置き・後書きは一切不要です。JSON のみを出力してください。\n"
            "確信が持てない項目は推測で埋めず、省略するか null / 空配列にしてください。\n\n"
            f"```json\n{schema_hint(schema)}\n```\n"
        )
        last_error = ""
        attempt_prompt = instruction
        for attempt in range(self.cfg.retries + 1):
            raw = self._run(attempt_prompt)
            try:
                return schema.model_validate(extract_json(raw))
            except (LLMError, ValidationError) as exc:
                last_error = str(exc)
                attempt_prompt = (
                    f"{instruction}\n\n"
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

    def _run(self, prompt: str) -> str:
        if shutil.which(self.cfg.command) is None:
            raise LLMError(
                f"'{self.cfg.command}' が見つかりません。"
                "`npm install -g @github/copilot` などで導入し、`comdiary doctor` で確認してください。"
            )
        try:
            proc = subprocess.run(
                self._argv(),
                input=prompt,
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

    def probe(self) -> tuple[bool, str]:
        """One cheap round trip, used by `comdiary doctor`."""
        try:
            self._run("Reply with exactly this and nothing else: COMDIARY_OK")
        except LLMError as exc:
            return False, str(exc)
        return True, f"model={self.cfg.model or 'auto'}"


class NullBackend:
    name = "none"

    def complete_json(self, prompt: str, schema: type[T]) -> T:
        raise LLMError(
            "LLM バックエンドが 'none' です。設定ファイルの [llm] backend を指定してください"
            " (場所は `comdiary config` で確認できます)。"
        )


def build_backend(cfg: LLMConfig) -> LLMBackend:
    if cfg.backend == "copilot":
        return CopilotBackend(cfg)
    if cfg.backend == "none":
        return NullBackend()
    if cfg.backend == "fake":
        from .fake import FakeBackend

        return FakeBackend(cfg)
    raise LLMError(f"未知の LLM バックエンド: {cfg.backend}")
