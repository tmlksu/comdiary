# データモデル

正本は `meetings/YYYY/MM/<slug>.json`。Markdown も SQLite 索引もここから作られます。
定義は `src/comdiary/models.py`。

## Meeting

```jsonc
{
  "schema_version": 1,
  "meeting_id": "b9e96d51998c",       // 原文 sha256 の先頭12桁 = 同じ内容なら同じ id
  "title": "昼会",
  "kind": "meeting",                   // meeting | chat | mail | note
  "date": "2026-08-13T12:00:00+09:00",
  "date_source": "filename",           // filename | body | llm | mtime | unknown
  "time_source": "body",               // null なら時刻不明 (date の 00:00 は placeholder)
  "attendees": ["田中 太郎", "鈴木 花子"],
  "summary": "...",
  "source_path": "G:/マイドライブ/memos/2026-08-13-1200-昼会.md",
  "source_sha256": "88c039a6...",
  "source_archive": "/home/x/ledger/sources/raw/2026/08/88c039a6-....md",
  "speaker_stats": { ... },
  "segments": [ ... ],
  "llm": { "backend": "copilot", "model": "auto" }
}
```

`date` は**日付と時刻を別々の出所から**決めます。ファイル名が `yyyy-mm-dd` だけの
ことが多く、日付ごと信用すると同じ日の会議が全部 00:00 になって順序が消えるためです。
どちらがどこから来たかを `date_source` / `time_source` に残します
(詳細は [known-issues.md](known-issues.md))。

## SpeakerStats

**Python が転記から計算します。LLM は関与しません。**
これが LLM の話者推定を信用してよいかの判断根拠になります (→ ADR 0004)。

```jsonc
{
  "distribution": { "田中 太郎": 0.33, "鈴木 花子": 0.33, "佐藤 健": 0.33 },
  "line_counts": { "田中 太郎": 4, ... },
  "total_lines": 12,
  "mic_mode": "per_speaker",        // shared | per_speaker | unknown
  "attribution": "reliable",         // reliable | uncertain
  "note": ""                         // uncertain の理由
}
```

## Segment

会議を話題で切った単位。**案件配下に投影されるのはこの単位**です。

```jsonc
{
  "segment_id": "s1",
  "title": "基盤移行の切替日",
  "summary": "...",
  "span": "昼会 2026-08-13",          // 転記中の位置の手がかり
  "guess": {                          // LLM の推定(そのまま採用はしない)
    "project_id": "alpha-migration",
    "confidence": 0.8,
    "rationale": "...",
    "suggested_name": null            // 既存に該当しない場合の新規案件名の案
  },
  "project_id": "alpha-migration",    // マッチャが決めた最終結果
  "match_method": "alias",            // alias | keyword | llm | manual | unmatched
  "decisions": [], "actions": [], "open_questions": [],
  "signals": [], "risks": [], "next_agenda": []
}
```

## Signal — 温度感

**特筆すべき場合だけ**記録します。淡々と進んだ議題では空配列が正常です。

```jsonc
{
  "kind": "resistance",
  "topic": "切替日",                  // 短い名詞句。集計のキーになる
  "speaker": "鈴木 花子",             // 帰属が不確実なら null に落とされる
  "intensity": "high",                // low | medium | high
  "evidence": "「それは無理でしょう」と語気を強め、過去の延伸にも言及した",
  "quote": "いや、それは無理でしょう",
  "concern": "テスト期間の不足と、過去に同じ約束が守られなかったこと",
  "confidence": 0.85
}
```

`kind` は固定語彙です。自由記述だと後から人・論点ごとに集計できません。

| kind | 意味 |
|---|---|
| `escalation` | 声を荒げた、語気が強まった |
| `resistance` | はっきり反対した、押し返した |
| `enthusiasm` | 前のめり、強い賛意 |
| `hesitation` | 言い淀んだ、条件付き同意 |
| `silence` | 意見を言うはずの人が黙った |
| `repetition` | 同じ主張を繰り返した(= 本人にとって重要) |
| `deflection` | 明言を避けた、判断を保留した |
| `concern` | 温度は高くないが明確な懸念 |

`topic` + `speaker` で集計されるので、**topic は短い名詞句に揃える**ことが効きます
(「納期」であって「納期が厳しいという話」ではない)。

## 他の要素

| 型 | フィールド |
|---|---|
| `Decision` | `what` / `rationale` / `decided_by` / `reversible` |
| `ActionItem` | `what` / `owner` / `due` / `status` (open, done, dropped) |
| `OpenQuestion` | `question` / `raised_by` / `blocks` / `status` |
| `Risk` | `what` / `impact` / `raised_by` |

`blocks` は「これが決まらないと何が止まるか」。優先順位付けに効くので入れてあります。

## registry

人が編集する YAML。ここだけは comdiary が勝手に書き換えません
(`project new` / `project alias` の明示的な操作を除く)。

```yaml
# registry/projects.yaml
- id: alpha-migration      # ASCII slug。以後変更しない
  name: Alpha基盤移行
  status: active           # active | paused | closed
  summary: 旧基盤から新基盤への移行
  aliases: [基盤移行, アルファ]     # ← 振り分け精度はここで決まる
  keywords: [切替日, 移行計画]      # ← 2個以上ヒットで採用
  members: [tanaka]
```

```yaml
# registry/people.yaml
- id: tanaka
  name: 田中 太郎
  aliases: [田中, Tanaka]    # 転記に出てくる表記を全部書く
  role: PM
```

```yaml
# registry/glossary.yaml — LLM に毎回渡される。略語を書くと精度が上がる
基盤移行: 2026年に予定している旧オンプレ環境から新環境への移行プロジェクト
```

## 索引 (`.comdiary/state.sqlite`)

すべて derived。消しても `comdiary reindex` で `meetings/*.json` から復元できます。

| テーブル | 用途 |
|---|---|
| `sources` | 取り込み済み判定 (sha256)、失敗記録 |
| `meetings` / `segments` | 一覧・時系列 |
| `signals` | `comdiary concerns` の集計元 |
| `items` | decisions / actions / questions / risks / agenda を横断で引く |
| `docs` (FTS5) | 全文検索 |
| `runs` | 実行履歴 |
