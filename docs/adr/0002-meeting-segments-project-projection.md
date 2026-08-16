# ADR 0002: 会議は「正本 + セグメント投影」で持つ

- 状態: 採用
- 日付: 2026-08-13

## 背景

昼会や定例では **1 回の会議で複数の案件が話される**。
案件ごとの living docs を作るには、会議を案件単位に切り分ける必要がある。

素直に「会議を案件ごとに分割して保存する」と、次が失われる。

- どの会議で話されたかという文脈(前後の話題、場の流れ)
- 分割を間違えたときに戻る先
- 「この日は何が話されたか」という時系列の一覧性

## 決定

会議は **1 本の正本** (`meetings/YYYY/MM/<slug>.md` + `.json`) として必ず保存する。
そのうえで、各セグメントを案件配下に **投影 (projection)** する。

```
meetings/2026/08/2026-08-13-1200-昼会-b9e96d.md     ← 正本(全セグメント)
  ├→ projects/alpha-migration/notes/meetings/...--s1.md   ← 抜粋 + 正本への逆リンク
  │  projects/alpha-migration/logs/2026-08.md             ← 1行サマリ(ブロック追記)
  │  projects/alpha-migration/open-questions.md           ← 未解決論点(ブロック追記)
  └→ projects/recruit-site/notes/meetings/...--s2.md
```

投影先の Markdown はすべて正本 JSON から再生成できる。二重管理ではない。

## 機械が書く領域の隔離

案件配下のファイルは、人も書き足す。そこで機械が書く塊を HTML コメントで囲む。

```markdown
<!-- comdiary:begin key=meeting/b9e96d51998c/s1 -->
...生成物...
<!-- comdiary:end key=meeting/b9e96d51998c/s1 -->
```

`key` が安定しているので、再実行は**自分が前に書いた塊を置換する**。
重複もしないし、人が枠の外に書いた文章には一切触れない。
本文が同一なら timestamp も更新しないので、意味のない git diff も出ない。

## 帰結

- 分割を間違えても正本を見れば全体が分かる。`comdiary assign` で貼り替えられる
- 未割当のセグメントは `_inbox/` に落ち、`comdiary triage` で人が確認する
- `projects/<id>/logs/YYYY-MM.md` が案件の時系列ビューになる
