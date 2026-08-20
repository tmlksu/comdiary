"""Configuration loading and discovery.

comdiary is meant to be installed as a standalone tool (``uv tool install``),
so it must work from any working directory without a checkout. Config is looked
up in a fixed order, and a ``conf.d/`` next to the chosen file can layer
machine-specific overrides on top of a shared base.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field

CONFIG_ENV = "COMDIARY_CONFIG"
HOME_ENV = "COMDIARY_HOME"
LEDGER_ENV = "COMDIARY_LEDGER"

CONFIG_NAME = "config.toml"
DOTDIR = ".comdiary"
OVERLAY_DIR = "conf.d"


def config_home() -> Path:
    """The per-user comdiary directory: ``$COMDIARY_HOME`` or ``~/.comdiary``."""
    if env := os.environ.get(HOME_ENV):
        return Path(env).expanduser()
    return Path.home() / DOTDIR


def xdg_config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "comdiary"
    if env := os.environ.get("XDG_CONFIG_HOME"):
        return Path(env).expanduser() / "comdiary"
    return Path.home() / ".config" / "comdiary"


class IngestConfig(BaseModel):
    inbox: Path | None = None
    done: Path | None = None
    failed: Path | None = None
    limit: int = 5
    glob: str = "*.md"
    #: A file must be untouched for this long before we take it. Sources that
    #: publish a finished document in one write only need enough of a margin for
    #: the file to land (e.g. a Drive stream mount syncing it); sources that
    #: append incrementally need longer than their write interval, or the
    #: transcript gets ingested truncated and nothing later reveals it.
    quiet_seconds: int = 60


class GeminiConfig(BaseModel):
    """Settings that only mean anything to the Gemini backend.

    Kept in its own table because ``command``/``extra_args`` are meaningless to
    an HTTP backend and these are meaningless to a CLI one; flattening them into
    ``[llm]`` would leave half the keys lying about what they affect.
    """

    #: The *name* of the environment variable holding the key — never the key.
    #: A config file gets copied between machines and into version control; an
    #: API key must not travel with it.
    api_key_env: str = "GEMINI_API_KEY"
    base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    max_output_tokens: int = 8192
    temperature: float | None = None
    #: 0 disables the model's thinking budget entirely; None leaves it to the
    #: model. Extraction is largely mechanical, so low budgets work well here.
    thinking_budget: int | None = None
    #: Upload the transcript once per document instead of with every call. One
    #: meeting costs 1 + len(segments) calls that all need the same transcript.
    cache: bool = True
    cache_ttl_seconds: int = 600
    #: Below the model's minimum cacheable size the create call only fails, so
    #: short transcripts skip it and are sent inline.
    cache_min_chars: int = 2000


class LLMConfig(BaseModel):
    backend: str = "copilot"  # copilot | gemini | fake | none
    #: "auto" lets Copilot choose; the Gemini backend reads it as "use my default".
    model: str = "auto"
    command: str = "copilot"
    extra_args: list[str] = Field(default_factory=list)
    timeout: int = 300
    retries: int = 2
    gemini: GeminiConfig = Field(default_factory=GeminiConfig)


class MatchConfig(BaseModel):
    #: LLM guesses below this land in _inbox for human triage instead of a project.
    min_confidence: float = 0.6
    #: Never invent projects unattended; `comdiary triage` is the intended path.
    auto_create_projects: bool = False


class GitConfig(BaseModel):
    enabled: bool = True
    #: Hard guard: the ledger holds private material and must not gain a remote.
    forbid_remote: bool = True


class Config(BaseModel):
    # Derived from config_home() so $COMDIARY_HOME relocates config *and* ledger
    # together; splitting them would put a ledger somewhere nobody asked for.
    ledger: Path = Field(default_factory=lambda: config_home() / "ledger")
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    match: MatchConfig = Field(default_factory=MatchConfig)
    git: GitConfig = Field(default_factory=GitConfig)
    source_path: Path | None = None
    overlays: list[Path] = Field(default_factory=list)

    def resolved(self) -> Config:
        self.ledger = Path(self.ledger).expanduser().resolve()
        for field in ("inbox", "done", "failed"):
            value = getattr(self.ingest, field)
            if value is not None:
                setattr(self.ingest, field, Path(value).expanduser())
        if self.ingest.failed is None and self.ingest.done is not None:
            self.ingest.failed = self.ingest.done.parent / "memos_failed"
        return self


def candidate_paths(cwd: Path | None = None) -> list[Path]:
    """Where a config may live, most specific first."""
    cwd = cwd or Path.cwd()
    paths: list[Path] = []
    if env := os.environ.get(CONFIG_ENV):
        paths.append(Path(env).expanduser())
    paths.append(cwd / "comdiary.toml")
    paths.append(cwd / DOTDIR / CONFIG_NAME)
    paths.append(config_home() / CONFIG_NAME)
    paths.append(xdg_config_dir() / CONFIG_NAME)
    # Deduplicate while preserving order — $COMDIARY_HOME may point at an XDG dir.
    seen: set[Path] = set()
    unique = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def default_config_path() -> Path:
    """Where `comdiary init` writes when nothing exists yet."""
    return config_home() / CONFIG_NAME


def overlay_paths(base: Path) -> list[Path]:
    """``conf.d/*.toml`` next to the base config, applied in sorted order."""
    directory = base.parent / OVERLAY_DIR
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob("*.toml") if p.is_file())


def deep_merge(base: dict, overlay: dict) -> dict:
    """Overlay wins per key; nested tables merge rather than replace."""
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def toml_string(value: str | Path) -> str:
    """Render a path (or any string) as a TOML string.

    Prefers a literal string (single quotes) because Windows paths are full of
    backslashes, and inside a TOML *basic* string ``C:\\Users`` is read as the
    escape ``\\U`` — which fails with "invalid hex value" rather than doing
    anything sensible. Literal strings have no escapes at all.
    """
    text = str(value)
    if "'" not in text and not any(c in text for c in "\n\r\x00"):
        return f"'{text}'"
    return json.dumps(text, ensure_ascii=False)


class ConfigError(ValueError):
    pass


def _read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"設定ファイルを読めませんでした: {path}\n"
            f"  {exc}\n"
            "  Windows のパスは \\ がエスケープとして解釈されます。"
            "シングルクォートで囲んでください:\n"
            "    inbox = 'G:\\マイドライブ\\memos'"
        ) from exc


def load_config(explicit: Path | None = None, cwd: Path | None = None) -> Config:
    if explicit is not None:
        if not explicit.is_file():
            raise FileNotFoundError(f"設定ファイルが見つかりません: {explicit}")
        paths = [explicit]
    else:
        paths = candidate_paths(cwd)

    for path in paths:
        if not path.is_file():
            continue
        data = _read_toml(path)
        overlays = overlay_paths(path)
        for overlay in overlays:
            data = deep_merge(data, _read_toml(overlay))
        cfg = Config.model_validate(data)
        cfg.source_path = path
        cfg.overlays = overlays
        return cfg.resolved()

    cfg = Config()
    if env := os.environ.get(LEDGER_ENV):
        cfg.ledger = Path(env).expanduser()
    return cfg.resolved()


EXAMPLE_TOML = """\
# comdiary の設定ファイル。
# 探索順 (最初に見つかったものを使用):
#   1. --config / $COMDIARY_CONFIG
#   2. ./comdiary.toml
#   3. ./.comdiary/config.toml
#   4. ~/.comdiary/config.toml          ($COMDIARY_HOME で変更可)
#   5. ~/.config/comdiary/config.toml   (Windows は %APPDATA%\\comdiary\\config.toml)
#
# 選ばれた設定ファイルと同じ場所に conf.d/*.toml があれば、
# ファイル名順に上書きマージされます (共通設定 + マシン別の差分)。

# 台帳の置き場所。手で開くことが多いなら見える場所を指定してください。
ledger = '~/.comdiary/ledger'

[ingest]
# 議事録が溜まるフォルダ。
# パスはシングルクォート(リテラル文字列)で囲んでください。ダブルクォートだと
# Windows の \\ がエスケープとして解釈され、'C:\\Users' などが読めなくなります。
#   inbox = 'G:\\マイドライブ\\meet-memos'
inbox  = '~/memos'
done   = '~/memos_done'
failed = '~/memos_failed'

# 1回の実行で処理する最大件数。
limit = 5
glob  = "*.md"

# 最終更新からこの秒数だけ静止したファイルだけを対象にします。
# 完成した文書を1回で書き出す経路なら、同期が届く程度の短い値で十分です。
# 少しずつ追記していく経路なら、その書き込み間隔より長くしてください
# (途中で掴むと議事録が切れたまま取り込まれ、後から気づけません)。
quiet_seconds = 60

[llm]
backend = "copilot"      # copilot | gemini | fake | none
model   = "auto"         # 使えるモデル名は `comdiary doctor --probe` で確認
command = "copilot"      # backend = "copilot" のときだけ使われます
extra_args = []
timeout = 300
retries = 2

# backend = "gemini" のときだけ使われます (Google AI Studio の API キー方式)。
# 従量課金なので、まず `comdiary run --dry-run` と `--llm fake` で配線を確かめてから
# 実キーに切り替えるのが安全です。
[llm.gemini]
# API キーは**ここに書かず**、環境変数に置いてその変数名だけを指定します。
# 設定ファイルはマシン間でコピーされるものなので、キーを載せると一緒に漏れます。
api_key_env = "GEMINI_API_KEY"
base_url = "https://generativelanguage.googleapis.com/v1beta"
max_output_tokens = 8192
# temperature = 0.2
# 抽出は機械的な作業なので thinking は絞って構いません (0 で無効)。
# thinking_budget = 0

# 議事録を1回だけアップロードし、セグメントごとの抽出で使い回します。
# 1会議あたり「1 + セグメント数」回の呼び出しが同じ全文を必要とするため、
# ここが従量課金でいちばん効くつまみです。
cache = true
cache_ttl_seconds = 600
cache_min_chars = 2000

[match]
# LLM の案件推定はこの確信度以上でのみ採用。下回ると _inbox に落ちます。
min_confidence = 0.6
# 無人実行中に案件を勝手に作らない。
auto_create_projects = false

[git]
enabled = true
# 台帳は非公開情報を含むためローカル専用。remote があるとコミットを拒否します。
forbid_remote = true
"""
