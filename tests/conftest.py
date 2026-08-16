from __future__ import annotations

from pathlib import Path

import pytest

from comdiary.config import Config, IngestConfig, LLMConfig
from comdiary.ledger.paths import LedgerPaths
from comdiary.ledger.writer import scaffold
from comdiary.models import Person, Project
from comdiary.registry.store import Registry

# All fixture content below is synthetic. This repository is intended to become
# public and must never contain real company material.

SAMPLE_LUNCH = """\
# 昼会 2026-08-13

田中 太郎: おはようございます。まず基盤移行の件から。
鈴木 花子: 切替日ですが、9月1日で本当に間に合いますか。テスト期間が2週間しかありません。
田中 太郎: 間に合わせます。移行計画はもう出してあります。
鈴木 花子: いや、それは無理でしょう。前回も同じことを言って延びました。
田中 太郎: ……検討します。
佐藤 健: 予備日を1週間取るのはどうでしょう。
鈴木 花子: それなら納得できます。

# 次に採用サイトのリニューアル

佐藤 健: デザイン案が3つ上がってきました。来週までにレビューをお願いします。
田中 太郎: 予算はいくらでしたっけ。
佐藤 健: 外注費で200万円です。
田中 太郎: 200万は高くないですか。前回は120万でしたよね。ここは詰めたいです。
佐藤 健: 見積もりを取り直します。
鈴木 花子: 公開時期はいつを想定していますか。
"""

SAMPLE_SHARED_MIC = """\
# 定例 2026-08-14

会議室A: 基盤移行の切替日について議論しました。9月1日は厳しいという意見が出ました。
会議室A: 予備日を確保する方向でまとまりそうです。
会議室A: 採用サイトの外注費についても触れました。
会議室A: 見積もりの取り直しを依頼します。
会議室A: 次回は8月21日です。
会議室A: 以上です。
田中 太郎: 了解しました。
"""


@pytest.fixture(autouse=True)
def isolate_user_config(tmp_path_factory, monkeypatch):
    """Never let a developer's real ~/.comdiary/config.toml leak into a test."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    for var in ("COMDIARY_CONFIG", "COMDIARY_HOME", "COMDIARY_LEDGER", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    return home


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    root = tmp_path / "ledger"
    scaffold(LedgerPaths(root))
    registry = Registry.load(root)
    registry.projects = [
        Project(
            id="alpha-migration",
            name="Alpha基盤移行",
            summary="旧基盤から新基盤への移行",
            aliases=["基盤移行", "アルファ"],
            keywords=["切替日", "移行計画"],
            members=["tanaka"],
        ),
        Project(
            id="recruit-site",
            name="採用サイトリニューアル",
            aliases=["採用サイト"],
            keywords=["デザイン案", "外注費"],
            members=["sato"],
        ),
    ]
    registry.people = [
        Person(id="tanaka", name="田中 太郎", aliases=["田中"], role="PM"),
        Person(id="suzuki", name="鈴木 花子", aliases=["鈴木"], role="QA"),
        Person(id="sato", name="佐藤 健", aliases=["佐藤"], role="デザイナー"),
    ]
    registry.save_projects()
    registry.save_people()
    return root


@pytest.fixture
def config(ledger: Path, tmp_path: Path) -> Config:
    inbox = tmp_path / "memos"
    done = tmp_path / "memos_done"
    failed = tmp_path / "memos_failed"
    for d in (inbox, done, failed):
        d.mkdir(parents=True, exist_ok=True)
    return Config(
        ledger=ledger,
        ingest=IngestConfig(inbox=inbox, done=done, failed=failed, limit=5, quiet_seconds=0),
        llm=LLMConfig(backend="fake"),
    ).resolved()
