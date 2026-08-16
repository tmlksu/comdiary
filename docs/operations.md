# 運用

## 定期実行

1時間おきに `comdiary ingest run` を回す想定です。
多重起動は可搬ロック (`.comdiary/ingest.lock`) で防いでいるので、
実行が重なっても壊れません(後から来たほうが即座に降ります)。

### Windows (タスク スケジューラ) — Drive ストリームを直接読む構成

Google Drive for Desktop のストリーム マウント (`G:\`) は Windows にしか生えないため、
Drive 上の文字起こしをそのまま読むならこの構成になります。

`%USERPROFILE%\.comdiary\config.toml`:

```toml
ledger = 'C:\Users\you\ledger'

[ingest]
# TOML のリテラル文字列(シングルクォート)にすると \ をエスケープせずに書けます
inbox  = 'G:\マイドライブ\meet-memos'
done   = 'G:\マイドライブ\meet-memos-done'
failed = 'C:\Users\you\comdiary-failed'
limit  = 5
quiet_seconds = 900
```

`run-comdiary.cmd`:

```bat
@echo off
comdiary ingest run >> C:\Users\you\comdiary-ingest.log 2>&1
```

`uv tool install` した `comdiary` は PATH から直接呼べるので、
作業ディレクトリを気にする必要はありません。

タスク スケジューラに登録:

```powershell
schtasks /create /tn "comdiary ingest" /tr "C:\Users\you\run-comdiary.cmd" ^
         /sc hourly /st 00:05 /rl LIMITED
```

注意点:

- **PC が起動している時だけ動きます。** 「タスクを可能な限り早く実行する」を
  有効にしておくと、起動後に取りこぼし分が流れます
- Drive ストリームはファイル実体を遅延取得します。`quiet_seconds` を効かせているので
  取り込み時には実体化済みのはずですが、初回は少し待たされることがあります
- `failed` はローカルに置くのを勧めます(Drive 上だと失敗ログまで共有領域に出ます)

### Linux (systemd timer)

Linux から Drive を読むには rclone マウント等が別途必要です
(→ [known-issues.md](known-issues.md) の「取り込みホスト」)。

`~/.config/systemd/user/comdiary.service`:

```ini
[Unit]
Description=comdiary ingest

[Service]
Type=oneshot
ExecStart=%h/.local/bin/comdiary ingest run
```

`~/.config/systemd/user/comdiary.timer`:

```ini
[Unit]
Description=comdiary ingest hourly

[Timer]
OnCalendar=hourly
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
```

```bash
systemctl --user enable --now comdiary.timer
loginctl enable-linger $USER   # ログアウト後も動かす
```

## 立ち上げ手順

```bash
uv tool install --with mcp git+https://github.com/tmlksu/comdiary

comdiary init                       # 台帳と設定ファイルを作る
comdiary config                     # 何がどこにできたか確認する
$EDITOR ~/.comdiary/config.toml     # 取り込み元パスなどを設定
comdiary doctor                     # 全部 OK になるまで直す

# 主要な案件を登録する。aliases / keywords が振り分け精度を決める
comdiary project new "Alpha基盤移行" --id alpha-migration \
    --alias 基盤移行 --alias アルファ --keyword 切替日 --keyword 移行計画

# 人物を登録すると concerns が人単位で引ける
$EDITOR ~/.comdiary/ledger/registry/people.yaml

# まず dry-run で、何が書かれるかを確認する
comdiary ingest run --dry-run
```

複数のマシンで同じ設定を使い回すなら、共通部分を `config.toml` に置き、
マシン固有の差分だけを `~/.comdiary/conf.d/10-<hostname>.toml` に書けます
(ファイル名順に上書きマージされます)。

**最初の数回は `--dry-run` で回して、セグメント分割と案件の振り分けを目で見てください。**
ここが合っていれば、あとは放っておいて育ちます。

## 日々の運用

```bash
comdiary ingest status      # 待ち・失敗の確認
comdiary triage             # 未割当セグメントの確認 (週1回程度)
comdiary questions          # 未解決の論点(横断)
```

### 振り分けを外したとき

一番効くのは**語を足すこと**です。LLM のプロンプトをいじるより確実に効きます。

```bash
comdiary project alias alpha-migration 基盤刷新 新環境
comdiary project alias alpha-migration --keyword カットオーバー
```

すでに `_inbox` に落ちたものは貼り替えます。

```bash
comdiary triage
comdiary assign b9e96d51998c s2 alpha-migration
```

### 取り込みに失敗したとき

失敗したファイルは `failed` フォルダに `.error.txt` 付きで移動します。
原因を直したら `inbox` に戻せば再処理されます(sha256 で「済み」判定されるのは成功分だけ)。

```bash
comdiary ingest status      # エラー内容を見る
mv ~/comdiary-failed/2026-08-13-昼会.md "G:/マイドライブ/meet-memos/"
```

## メンテナンス

```bash
comdiary reindex     # 索引を作り直す (.comdiary/state.sqlite は消してよい)
comdiary rerender    # テンプレートを変えた後、全 Markdown を作り直す
```

どちらも `meetings/*.json` から復元するので、**JSON さえ残っていれば全部戻ります**。
バックアップ対象として本当に重要なのは `meetings/` と `registry/`、
それに `sources/raw/` (原文) です。

## 台帳のバックアップ

台帳はローカル専用で remote を持ちません (→ ADR 0005)。
バックアップは外部媒体や別ディスクへの同期で担保してください。

```bash
rsync -a --delete ~/.comdiary/ledger/ /mnt/backup/ledger/
```

## LLM バックエンドを差し替える

`[llm] backend` を変えるだけです。`comdiary rerender` は LLM を呼ばないので、
過去分はそのまま使えます。

```toml
[llm]
backend = "copilot"     # copilot | fake | none
model   = "auto"          # 使えるモデル名は doctor --probe で確認
command = "copilot"
extra_args = []          # Copilot CLI の版に応じて調整
timeout = 300
retries = 2
```

`fake` はオフラインの決定論バックエンドで、見出しで機械的に分割するだけです。
パイプラインの配線確認や、LLM を呼ばずに台帳の形を確かめたいときに使えます。
