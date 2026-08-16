from __future__ import annotations

from pathlib import Path

from comdiary.ledger import blocks


def test_upsert_appends_then_replaces():
    text, changed = blocks.upsert("# 見出し\n", "k1", "最初の内容")
    assert changed and "最初の内容" in text

    text2, changed2 = blocks.upsert(text, "k1", "更新後の内容")
    assert changed2
    assert "最初の内容" not in text2
    assert text2.count("comdiary:begin") == 1


def test_identical_body_does_not_churn():
    text, _ = blocks.upsert("", "k1", "同じ内容")
    text2, changed = blocks.upsert(text, "k1", "同じ内容")
    assert not changed
    assert text2 == text


def test_human_prose_outside_blocks_survives():
    text, _ = blocks.upsert("# ログ\n", "k1", "機械が書いた行")
    text += "\n人が後から書いた大事なメモ\n"
    text, _ = blocks.upsert(text, "k1", "機械が書き直した行")
    assert "人が後から書いた大事なメモ" in text
    assert "機械が書いた行" not in text


def test_multiple_keys_coexist():
    text, _ = blocks.upsert("", "a", "A")
    text, _ = blocks.upsert(text, "b", "B")
    text, _ = blocks.upsert(text, "a", "A2")
    keys = {b.key for b in blocks.find_blocks(text)}
    assert keys == {"a", "b"}
    assert "A2" in text and "B" in text


def test_upsert_file_creates_with_header(tmp_path: Path):
    path = tmp_path / "logs" / "2026-08.md"
    assert blocks.upsert_file(path, "k", "本文", header="# ログ\n")
    assert path.read_text(encoding="utf-8").startswith("# ログ")
    assert not blocks.upsert_file(path, "k", "本文", header="# ログ\n")


def test_strip_blocks_returns_human_text():
    text, _ = blocks.upsert("人の文章\n", "k", "機械の文章")
    assert blocks.strip_blocks(text) == "人の文章"
