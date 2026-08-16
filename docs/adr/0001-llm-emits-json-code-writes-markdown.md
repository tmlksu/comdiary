# ADR 0001: LLM は JSON だけを返し、Markdown はコードが書く

- 状態: 採用
- 日付: 2026-08-13

## 背景

議事録から living docs を育てる仕組みでは、素直に考えると
「LLM に既存の Markdown を読ませて、更新後の Markdown を書かせる」形になる。

## 決定

LLM の出力は **pydantic で検証された JSON のみ**とする。
Markdown は Python が Jinja テンプレートから決定論的に生成する。

## 理由

Markdown を LLM に書かせると、次が全部壊れる。

1. **冪等性** — 同じ議事録を 2 回入れると、文面が微妙に違う 2 つの記述が残る
2. **レビュー性** — git diff が「文章が書き換わった」としか言わず、何が変わったか読めない
3. **過去の保全** — 追記のつもりで既存の記述を要約・削除してしまう事故が起きる
4. **スキーマ進化** — 出力形式を変えたくなったとき、過去分を作り直す手段が無い

JSON を正本にすると、すべて解決する。`meetings/*.json` が唯一の真実で、
Markdown はその投影にすぎない。`comdiary rerender` でいつでも作り直せる。

## 帰結

- `meetings/<slug>.json` が正本。`.md` は生成物であり、手で編集しない
- 案件配下の Markdown は、機械が書く領域を HTML コメントで囲む(→ ADR 0002)
- テンプレートを変えたら `comdiary rerender` を流せば全台帳に反映される
- LLM がスキーマを外したら pydantic が弾き、エラーを添えて再試行する
  (`llm/backend.py` の `_RetryingBackend`)
