# サンプル

ここにあるものはすべて**合成データ**です。実在の会議・人物ではありません。
このリポジトリは公開を前提としているため、会社データは一切含めません。

## 動かしてみる

LLM を呼ばずに、パイプラインの配線と台帳の形だけ確かめられます。

`$COMDIARY_HOME` を使えば、自分の `~/.comdiary` に一切触れずに試せます。

```bash
export COMDIARY_HOME=/tmp/comdiary-demo/.comdiary
mkdir -p /tmp/comdiary-demo/memos /tmp/comdiary-demo/memos_done
cp examples/memos/*.md /tmp/comdiary-demo/memos/

uv run comdiary init
cat > "$COMDIARY_HOME/config.toml" <<'TOML'
ledger = "/tmp/comdiary-demo/.comdiary/ledger"
[ingest]
inbox = "/tmp/comdiary-demo/memos"
done  = "/tmp/comdiary-demo/memos_done"
quiet_seconds = 0
[llm]
backend = "fake"
TOML

uv run comdiary config          # どの設定が読まれているか確認
uv run comdiary project new "Alpha基盤移行" --id alpha-migration \
    --alias 基盤移行 --keyword 切替日 --keyword 移行計画
uv run comdiary project new "採用サイトリニューアル" --id recruit-site \
    --alias 採用サイト --keyword デザイン案 --keyword 外注費

uv run comdiary ingest run
tree "$COMDIARY_HOME/ledger"
```

1本の昼会が2つの案件に振り分けられ、それぞれの `notes/` と `logs/` に
投影されているのが確認できます。

`fake` バックエンドは見出しで機械的に分割するだけなので、要約や温度感は入りません。
実際の抽出結果を見るには `[llm] backend = "copilot"` にしてください。
