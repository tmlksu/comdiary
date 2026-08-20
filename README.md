# comdiary

会社のミーティング・チャット・メールを **docs as code** の台帳(ledger)に落とし込み、
CLI と薄い MCP サーバから引けるようにするツール。

議事録を放り込んでおくと、案件ごとに `adr / spec / notes / logs / open-questions / handoff`
が育っていき、資料作成・アジェンダ作成・タスク整理・壁打ちのときに
「その案件でこれまで何が決まって、何が残っていて、誰が何を気にしているか」を
LLM に渡せる状態になります。

```
memos/ (Google Meet の議事録が溜まる)
   │
   │  comdiary ingest run   ← 1時間おきに最大N件
   ▼
会議の正本 meetings/2026/08/....md + .json
   │
   │  1つの昼会が複数案件を含むので、セグメントに分割して投影
   ▼
projects/<案件>/notes/ logs/ open-questions.md ...
   │
   │  comdiary context / search / concerns  ─┐
   ▼                                          ├→ LLM (CLI or MCP)
SQLite FTS index                             ─┘
```

## 設計の芯

**LLM に Markdown を書かせない。** LLM は検証済みの JSON を返すだけで、
Markdown は Python が決定論的にレンダリングします。だから台帳は再生成でき、
差分がレビューでき、同じ議事録を2回入れても壊れません。

| 決めごと | 理由 |
|---|---|
| 会議の正本(JSON)が唯一の真実、Markdown はその投影 | スキーマを変えても `rerender` で全部作り直せる |
| 機械が書く領域は HTML コメントで囲む | 人が同じファイルに書いた文章を絶対に消さない |
| 案件の自動作成をしない | 誤った案件が台帳に混ざると回復が面倒。`triage` で人が確認する |
| 話者比率を Python で計算する | 共通マイクだと LLM の話者推定が壊れる。それを機械的に検知して打ち消す |
| 原文アーカイブ → 処理 → 最後に移動 | 途中で落ちても入力が失われず、再実行できる |
| ベクトル検索を使わない | 案件10件規模では FTS で十分。低スペックな常時稼働機でも動く |

## インストール

```bash
uv tool install --with mcp git+https://github.com/tmlksu/comdiary
comdiary --help
```

リポジトリを持ってきて開発する場合は `uv sync --all-extras` → `uv run comdiary`。

### LLM バックエンド

既定は GitHub Copilot CLI です。定額契約があるならこれが一番安く済みます。

```bash
npm install -g @github/copilot
comdiary doctor            # 設定・依存・台帳の点検
comdiary doctor --probe    # モデル名まで実地確認 (LLM を1回呼びます)
```

`[llm] model` に指定したモデル名が使えるかは実際に呼ぶまで分かりません
(Copilot CLI は未知のモデル名でも終了コード 0 で失敗します)。`--probe` はそこを見ます。
既定の `auto` なら Copilot が選びます。

Gemini API (Google AI Studio) も使えます。API キーは設定ファイルではなく環境変数に置き、
設定には**変数名だけ**を書きます。

```bash
export GEMINI_API_KEY=...
comdiary run --llm gemini
```

```toml
[llm]
backend = "gemini"
model   = "gemini-2.5-flash"

[llm.gemini]
api_key_env = "GEMINI_API_KEY"
```

従量課金なので、コストは呼び出し回数ではなく**送ったトークン数**で決まります。
1会議あたり「1 (分割) + セグメント数 (抽出)」回の呼び出しが同じ議事録全文を必要とするため、
Gemini バックエンドは既定で議事録を1回だけアップロードして使い回し
(context caching)、JSON Schema はプロンプトに書かず `responseSchema` で直接渡します
(→ [docs/operations.md](docs/operations.md#llm-バックエンドを差し替える))。

## セットアップ

```bash
comdiary init                 # 台帳ツリーと設定ファイルを作る
comdiary config               # どこに何ができたか確認する
$EDITOR ~/.comdiary/config.toml
comdiary project new "Alpha基盤移行" --id alpha-migration \
    --alias 基盤移行 --keyword 切替日 --keyword 移行計画
```

`registry/projects.yaml` の `aliases` / `keywords` が案件振り分けの精度を決めます。
振り分けを外したら `comdiary project alias` で語を足すのが一番効きます。

### 設定ファイルの場所

最初に見つかったものを使います。

| 順 | パス | 用途 |
|---|---|---|
| 1 | `--config` / `$COMDIARY_CONFIG` | 明示指定 |
| 2 | `./comdiary.toml` | そのディレクトリ限定 |
| 3 | `./.comdiary/config.toml` | 同上 (dotdir 派) |
| 4 | `~/.comdiary/config.toml` | **既定。`$COMDIARY_HOME` で移動可** |
| 5 | `~/.config/comdiary/config.toml` | XDG (Windows は `%APPDATA%\comdiary\`) |

選ばれた設定と同じ場所に `conf.d/*.toml` があれば、ファイル名順に**上書きマージ**されます。
共通設定を1つ置いて、マシン固有の差分だけを `conf.d/10-local.toml` に書く使い方ができます。

```
~/.comdiary/
├── config.toml            # 共通
├── conf.d/
│   └── 10-thishost.toml   # [ingest] inbox だけこのマシン用に差し替え
└── ledger/                # 既定の台帳 (config で移動可)
```

## 使う

```bash
# 取り込み (1時間おきに実行する想定)
comdiary ingest run --limit 5
comdiary ingest run --dry-run          # 何が書かれるか確認するだけ
comdiary ingest status                 # 待ち・失敗の確認

# 後からチャット・メールを入れる
comdiary ingest add ./slack-export.txt --kind chat -p alpha-migration
pbpaste | comdiary ingest add - --kind mail -p alpha-migration

# 未割当の振り分け
comdiary triage
comdiary assign <meeting_id> s2 alpha-migration

# 読む
comdiary project show alpha-migration
comdiary search 切替日 -p alpha-migration
comdiary topics                        # 案件をまたぐ論点(まだ案件でない課題を探す)
comdiary topics --show 属人化          # 1つの論点を掘り下げる
comdiary questions                     # 未解決の論点(横断)
comdiary actions -p alpha-migration    # 未完了のアクション
comdiary concerns --person 鈴木        # 誰が何を気にしているか

# LLM に渡す
comdiary context alpha-migration --purpose material   # 資料作成用
comdiary context alpha-migration --purpose agenda     # 次回アジェンダ用
comdiary context alpha-migration --json | llm ...
```

すべての読み取りコマンドに `--json` があります。

## MCP

```bash
uv sync --extra mcp
comdiary mcp   # stdio
```

| tool | 用途 |
|---|---|
| `project_list` | 案件一覧 |
| `context_pack` | 現状+未解決+関心事をまとめて取得(まずこれ) |
| `ledger_search` | 全文検索 |
| `meeting_get` | 会議の正本 |
| `open_questions` / `open_actions` | 未解決・未完了 |
| `person_concerns` | 誰が何を気にしているか |
| `topics` / `topic_get` | 案件をまたぐ論点(点をつなぐ起点) |
| `timeline` | 案件の時系列 |
| `append_note` | 追記(唯一の書き込み) |

## 温度感(signals)

議事録から、**特筆すべき場合だけ**「誰が何にどう反応したか」を記録します。
`escalation / resistance / enthusiasm / hesitation / silence / repetition / deflection / concern`
の固定語彙で持つので、後から人・論点ごとに集計できます。

共通マイク(1台のマイクで会議室全体を拾う構成)だと話者ラベルが1人に偏り、
LLM の話者推定は当てになりません。comdiary は発言比率を機械的に計算して
この状況を検知し、**「何が論点だったか」は残したまま「誰が言ったか」を落とします。**
間違った人物に感情を紐づけるくらいなら、話者不明のほうがましだからです。

## 台帳の置き場所

台帳は社内情報を含むため **ローカル専用**です。
`comdiary` は台帳リポジトリに remote が設定されていると **コミットを拒否します**
(`[git] forbid_remote = false` で解除可能)。

このツール本体のリポジトリには会社データを一切含めません。
テストの題材はすべて合成データです。

## ドキュメント

- [docs/adr/](docs/adr/) — 方針転換級の決定
- [docs/known-issues.md](docs/known-issues.md) — 既知の制約と環境ごとの調整
- [docs/schema.md](docs/schema.md) — データモデル
- [docs/operations.md](docs/operations.md) — 定期実行の設定

## ライセンス

MIT
