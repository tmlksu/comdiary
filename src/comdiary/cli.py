from __future__ import annotations

import json
import shutil
import sys
from contextlib import closing
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import (
    CONFIG_NAME,
    DOTDIR,
    EXAMPLE_TOML,
    Config,
    ConfigError,
    candidate_paths,
    default_config_path,
    load_config,
    toml_string,
)
from .context import PURPOSES, aggregate_concerns, aggregate_topics, build_pack, topic_detail
from .ingest.pipeline import IngestOutcome, Pipeline, RunReport
from .ingest.sources import discover, iter_paths, load_doc
from .ingest.state import State
from .ledger import git as gitops
from .ledger.paths import SECTIONS, LedgerPaths
from .ledger.writer import LedgerWriter, scaffold
from .llm.backend import LLMError, build_backend
from .lock import LockBusy, file_lock
from .models import Meeting, Project
from .registry.store import Registry
from .util import atomic_write, now, sha256_text, slugify_ascii

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="会議・チャット・メールの docs-as-code 台帳 (ledger) を作り、CLI と MCP から引く。",
)
project_app = typer.Typer(no_args_is_help=True, help="案件台帳の操作")
ingest_app = typer.Typer(no_args_is_help=True, help="文書の取り込み")
app.add_typer(project_app, name="project")
app.add_typer(ingest_app, name="ingest")

console = Console()
err = Console(stderr=True)

ConfigOpt = Annotated[Path | None, typer.Option("--config", "-c", help="comdiary.toml のパス")]
JsonOpt = Annotated[bool, typer.Option("--json", help="JSON で出力 (LLM 向け)")]


class Ctx:
    config: Config

    @classmethod
    def load(cls, path: Path | None, llm_override: str | None = None) -> Config:
        try:
            cfg = load_config(path)
        except (FileNotFoundError, ConfigError) as exc:
            _fail(str(exc))
            raise  # unreachable; _fail raises
        if llm_override:
            cfg.llm.backend = llm_override
        return cfg


def _fail(message: str) -> None:
    err.print(f"[red]エラー[/red] {message}")
    raise typer.Exit(1)


def _emit(data, as_json: bool, render) -> None:
    if as_json:
        console.print_json(json.dumps(data, ensure_ascii=False, default=str))
    else:
        render()


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------


@app.command()
def version() -> None:
    """バージョンを表示する。"""
    console.print(f"comdiary {__version__}")


@app.command()
def init(
    config: ConfigOpt = None,
    ledger: Annotated[Path | None, typer.Option(help="台帳のルート")] = None,
    git: Annotated[bool, typer.Option(help="台帳を git リポジトリとして初期化する")] = True,
    write_config: Annotated[bool, typer.Option(help="設定ファイルが無ければ雛形を作る")] = True,
    here: Annotated[
        bool, typer.Option("--here", help="設定をユーザ共通ではなくカレントの .comdiary/ に置く")
    ] = False,
) -> None:
    """台帳ツリーと設定ファイルを作る。"""
    cfg = Ctx.load(config)
    root = (ledger or cfg.ledger).expanduser().resolve()
    paths = LedgerPaths(root)
    created = scaffold(paths)
    console.print(f"[green]台帳を初期化しました[/green] {root}")
    for path in created:
        console.print(f"  + {path.relative_to(root) if path != root else path}")

    if git:
        result = gitops.init(root)
        console.print(f"  git: {result.message.strip() or 'ok'}")
        console.print(
            "  [yellow]注意[/yellow] この台帳はローカル専用です。remote を追加しないでください。"
        )

    if not write_config:
        return
    if cfg.source_path is not None:
        console.print(f"  設定ファイルは既にあります: {cfg.source_path}")
        return
    target = (Path.cwd() / DOTDIR / CONFIG_NAME) if here else default_config_path()
    atomic_write(
        target,
        EXAMPLE_TOML.replace("ledger = '~/.comdiary/ledger'", f"ledger = {toml_string(root)}"),
    )
    console.print(f"  + {target} (取り込み元パスを編集してください)")


@app.command("config")
def config_cmd(config: ConfigOpt = None, as_json: JsonOpt = False) -> None:
    """設定がどこから読まれているかと、現在の値を表示する。"""
    cfg = Ctx.load(config)
    data = cfg.model_dump(mode="json")

    def render() -> None:
        if cfg.source_path:
            console.print(f"[green]使用中[/green] {cfg.source_path}")
            for overlay in cfg.overlays:
                console.print(f"  + 上書き {overlay}")
        else:
            console.print("[yellow]設定ファイルが見つかりません(既定値で動作中)[/yellow]")
            console.print(f"  `comdiary init` で {default_config_path()} に作成できます")
        console.print("\n探索順:")
        for path in candidate_paths():
            mark = "[green]✓[/green]" if path.is_file() else " "
            console.print(f"  {mark} {path}")
        console.print("\n現在の値:")
        console.print(f"  ledger        {cfg.ledger}")
        console.print(f"  ingest.inbox  {cfg.ingest.inbox or '(未設定)'}")
        console.print(f"  ingest.done   {cfg.ingest.done or '(未設定)'}")
        console.print(f"  llm           {cfg.llm.backend} / {cfg.llm.model}")

    _emit(data, as_json, render)


@app.command()
def doctor(
    config: ConfigOpt = None,
    probe_llm: Annotated[
        bool, typer.Option("--probe", help="LLM を1回呼んでモデル名まで確認する")
    ] = False,
    llm: Annotated[
        str | None,
        typer.Option("--llm", help="backend を一時的に上書き (設定を書き換える前の下見に)"),
    ] = None,
) -> None:
    """設定・依存・台帳の健全性を点検する。"""
    cfg = Ctx.load(config, llm)
    paths = LedgerPaths(cfg.ledger)
    ok = True

    def check(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        mark = "[green]OK  [/green]" if good else "[red]NG  [/red]"
        ok = ok and good
        console.print(f"{mark} {label}" + (f" — {detail}" if detail else ""))

    if cfg.source_path:
        console.print(f"設定ファイル: {cfg.source_path}")
        for overlay in cfg.overlays:
            console.print(f"  + 上書き: {overlay}")
    else:
        console.print("設定ファイル: (見つからないため既定値で動作中)")
        console.print(f"  `comdiary init` で {default_config_path()} に作成できます")
    check("台帳ディレクトリ", paths.root.is_dir(), str(paths.root))
    check("registry/projects.yaml", (paths.registry / "projects.yaml").is_file())

    if cfg.ingest.inbox:
        check("取り込み元 (inbox)", cfg.ingest.inbox.is_dir(), str(cfg.ingest.inbox))
    else:
        check("取り込み元 (inbox)", False, "[ingest] inbox が未設定")
    if cfg.ingest.done:
        check("完了先 (done)", True, str(cfg.ingest.done))

    # What "configured correctly" means differs per backend — a CLI needs its
    # binary on PATH, an API needs a key in the environment — so each backend
    # answers for itself rather than doctor knowing them all.
    try:
        backend = build_backend(cfg.llm)
    except LLMError as exc:
        backend = None
        check(f"LLM バックエンド '{cfg.llm.backend}'", False, str(exc))
    if backend is not None:
        ready, detail = backend.preflight()
        check(f"LLM バックエンド '{backend.name}'", ready, detail)
        if ready and probe_llm:
            # Copilot reports a wrong --model name with a zero exit code, and an
            # API key is only really valid once it has been accepted, so the only
            # honest check is one real round trip.
            good, detail = backend.probe()
            check(f"LLM 応答 (model={cfg.llm.model or 'auto'})", good, detail)
        elif ready and backend.name not in ("none", "fake"):
            console.print(
                "[dim]     モデル名の実地確認は --probe を付けると行います"
                " (LLM を実際に1回呼ぶため、従量課金のバックエンドでは課金されます)[/dim]"
            )
        backend.close()

    check("git", shutil.which("git") is not None)
    if gitops.is_repo(paths.root):
        found = gitops.remotes(paths.root)
        check(
            "台帳にリモートが無いこと",
            not found,
            "リモートあり: " + ", ".join(found) if found else "ローカル専用",
        )

    try:
        registry = Registry.load(paths.root)
        check(
            "案件台帳の読み込み",
            True,
            f"{len(registry.active_projects())} active / {len(registry.projects)} total",
        )
    except Exception as exc:  # noqa: BLE001
        check("案件台帳の読み込み", False, str(exc))

    if paths.state_db.is_file():
        with State(paths.state_db) as state:
            console.print(f"     index: {state.stats()}")

    raise typer.Exit(0 if ok else 1)


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


@project_app.command("list")
def project_list(
    config: ConfigOpt = None,
    as_json: JsonOpt = False,
    all_: Annotated[bool, typer.Option("--all", help="closed も表示")] = False,
) -> None:
    """案件の一覧。"""
    cfg = Ctx.load(config)
    registry = Registry.load(cfg.ledger)
    items = registry.projects if all_ else [p for p in registry.projects if p.status != "closed"]
    data = [p.model_dump(mode="json") for p in items]

    def render() -> None:
        table = Table("id", "名前", "状態", "別名", "概要")
        for p in items:
            table.add_row(p.id, p.name, p.status, ", ".join(p.aliases), p.summary[:40])
        console.print(table if items else "[yellow]案件が登録されていません[/yellow]")

    _emit(data, as_json, render)


@project_app.command("new")
def project_new(
    name: Annotated[str, typer.Argument(help="案件名")],
    id_: Annotated[str | None, typer.Option("--id", help="ASCII slug。省略時は名前から生成")] = None,
    summary: Annotated[str, typer.Option("--summary", help="概要")] = "",
    alias: Annotated[list[str], typer.Option("--alias", help="別名 (複数可)")] = [],
    keyword: Annotated[list[str], typer.Option("--keyword", help="キーワード (複数可)")] = [],
    config: ConfigOpt = None,
) -> None:
    """案件を台帳に追加し、ディレクトリを作る。"""
    cfg = Ctx.load(config)
    registry = Registry.load(cfg.ledger)
    project_id = id_ or slugify_ascii(name, fallback="")
    if not project_id:
        _fail("案件名が非 ASCII のため id を自動生成できません。--id で指定してください。")
    if registry.project(project_id):
        _fail(f"id '{project_id}' は既に存在します")

    project = Project(
        id=project_id, name=name, summary=summary, aliases=list(alias), keywords=list(keyword)
    )
    registry.add_project(project)
    LedgerWriter(LedgerPaths(cfg.ledger)).ensure_project(project)
    console.print(f"[green]追加しました[/green] {project_id} — {name}")


@project_app.command("alias")
def project_alias(
    project_id: str,
    alias: Annotated[list[str], typer.Argument(help="追加する別名")],
    keyword: Annotated[list[str], typer.Option("--keyword", help="別名ではなくキーワードとして追加")] = [],
    config: ConfigOpt = None,
) -> None:
    """既存案件に別名・キーワードを足す (振り分け精度の調整に使う)。"""
    cfg = Ctx.load(config)
    registry = Registry.load(cfg.ledger)
    project = registry.project(project_id)
    if not project:
        _fail(f"案件 '{project_id}' が見つかりません")
    for a in alias:
        if a not in project.aliases:
            project.aliases.append(a)
    for k in keyword:
        if k not in project.keywords:
            project.keywords.append(k)
    registry.save_projects()
    console.print(f"[green]更新しました[/green] aliases={project.aliases} keywords={project.keywords}")


@project_app.command("show")
def project_show(project_id: str, config: ConfigOpt = None, as_json: JsonOpt = False) -> None:
    """案件の現在地をまとめて表示する。"""
    cfg = Ctx.load(config)
    registry = Registry.load(cfg.ledger)
    project = registry.project(project_id)
    if not project:
        _fail(f"案件 '{project_id}' が見つかりません")
    pack = build_pack(cfg.ledger, project_id, purpose="general")
    _emit(pack.to_dict(), as_json, lambda: console.print(pack.to_markdown()))


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


def _print_report(report: RunReport) -> None:
    if report.dry_run:
        console.print("[yellow]--dry-run: ファイルは書き込まれていません[/yellow]")
    for outcome in report.outcomes:
        _print_outcome(outcome)
    if report.warming:
        console.print(
            f"[dim]書き込み中とみなしてスキップ: {len(report.warming)} 件 "
            f"(quiet_seconds 未満)[/dim]"
        )
    console.print(f"完了: 成功 {report.processed} / 失敗 {report.failed}")


def _print_outcome(outcome: IngestOutcome) -> None:
    name = outcome.doc.name
    if outcome.skipped:
        console.print(f"[dim]skip[/dim] {name} — {outcome.skipped}")
        return
    if outcome.error:
        console.print(f"[red]fail[/red] {name} — {outcome.error.splitlines()[0]}")
        return
    meeting = outcome.meeting
    assert meeting and outcome.write
    console.print(f"[green]ok[/green]   {name} → {meeting.title} ({meeting.date:%Y-%m-%d %H:%M})")
    console.print(
        f"       マイク: {meeting.speaker_stats.mic_mode} / "
        f"話者帰属: {meeting.speaker_stats.attribution} / "
        f"日付: {meeting.date_source} / 時刻: {meeting.time_source or '不明'} / "
        f"LLM呼び出し: {outcome.llm_calls}"
    )
    if meeting.date_source in ("mtime", "unknown"):
        console.print(
            "       [yellow]警告[/yellow] 会議日時をファイル名からも本文からも読めず、"
            "ファイルの更新時刻を使いました。\n"
            "              文字起こしが会議より後に書き出される経路では、これは会議の時刻ではありません。"
        )
    for seg in meeting.segments:
        target = seg.project_id or "[yellow]_inbox[/yellow]"
        counts = (
            f"決定{len(seg.decisions)} 行動{len(seg.actions)} "
            f"論点{len(seg.open_questions)} 温度{len(seg.signals)}"
        )
        console.print(f"       - {seg.segment_id} {seg.title} → {target} ({seg.match_method}) {counts}")
    for path in outcome.write.changed:
        console.print(f"         [dim]~ {path}[/dim]")


@ingest_app.command("run")
def ingest_run(
    limit: Annotated[int | None, typer.Option("--limit", "-n", help="1回で処理する最大件数")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="書き込まずに結果だけ表示")] = False,
    llm: Annotated[
        str | None, typer.Option("--llm", help="backend を一時的に上書き (gemini / fake 等)")
    ] = None,
    commit: Annotated[bool, typer.Option(help="台帳を git コミットする")] = True,
    config: ConfigOpt = None,
) -> None:
    """取り込み元フォルダを見に行き、未処理の議事録を処理する。"""
    cfg = Ctx.load(config, llm)
    paths = LedgerPaths(cfg.ledger)
    if not paths.root.is_dir():
        _fail(f"台帳がありません: {paths.root} — `comdiary init` を実行してください")

    try:
        with file_lock(paths.internal / "ingest.lock"):
            with State(paths.state_db) as state, closing(build_backend(cfg.llm)) as backend:
                pipeline = Pipeline(cfg, backend, state, dry_run=dry_run)
                report = pipeline.run(limit)
            _print_report(report)
            if commit and not dry_run and report.processed and cfg.git.enabled:
                _commit(cfg, f"ingest: {report.processed} 件の議事録を取り込み")
    except LockBusy as exc:
        _fail(str(exc))
    except FileNotFoundError as exc:
        _fail(str(exc))


@ingest_app.command("add")
def ingest_add(
    target: Annotated[str, typer.Argument(help="ファイル / フォルダ / '-' で標準入力")],
    kind: Annotated[str, typer.Option("--kind", help="meeting | chat | mail | note")] = "note",
    project: Annotated[str | None, typer.Option("--project", "-p", help="案件を固定する")] = None,
    title: Annotated[str | None, typer.Option("--title", help="標準入力時のタイトル")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    llm: Annotated[str | None, typer.Option("--llm")] = None,
    commit: Annotated[bool, typer.Option(help="台帳を git コミットする")] = True,
    config: ConfigOpt = None,
) -> None:
    """チャットログ・メール・メモを後から台帳に入れる。"""
    cfg = Ctx.load(config, llm)
    paths = LedgerPaths(cfg.ledger)
    if kind not in {"meeting", "chat", "mail", "note"}:
        _fail(f"未知の kind: {kind}")

    docs = []
    if target == "-":
        text = sys.stdin.read()
        if not text.strip():
            _fail("標準入力が空です")
        from .ingest.sources import SourceDoc

        name = title or f"stdin-{now():%Y%m%d-%H%M%S}"
        docs.append(
            SourceDoc(
                path=Path(name),
                name=name,
                text=text,
                sha256=sha256_text(text),
                kind=kind,
                mtime=now().timestamp(),
            )
        )
    else:
        for path in iter_paths(Path(target).expanduser()):
            docs.append(load_doc(path, kind=kind))
    if not docs:
        _fail("取り込める文書がありませんでした")

    with State(paths.state_db) as state, closing(build_backend(cfg.llm)) as backend:
        pipeline = Pipeline(cfg, backend, state, dry_run=dry_run)
        processed = 0
        for doc in docs:
            outcome = pipeline.ingest_doc(doc, project_hint=project)
            _print_outcome(outcome)
            if not dry_run:
                pipeline._record(outcome)  # noqa: SLF001 - same package, intentional
            processed += 1 if outcome.ok else 0

    if commit and not dry_run and processed and cfg.git.enabled:
        _commit(cfg, f"ingest: {kind} を {processed} 件追加")


@ingest_app.command("status")
def ingest_status(config: ConfigOpt = None, as_json: JsonOpt = False) -> None:
    """取り込み待ち・失敗の状況。"""
    cfg = Ctx.load(config)
    paths = LedgerPaths(cfg.ledger)
    ready: list[Path] = []
    warming: list[Path] = []
    if cfg.ingest.inbox and cfg.ingest.inbox.is_dir():
        ready, warming = discover(
            cfg.ingest.inbox, cfg.ingest.glob, limit=10_000, quiet_seconds=cfg.ingest.quiet_seconds
        )
    with State(paths.state_db) as state:
        failures = [dict(r) for r in state.failures()]
        stats = state.stats()

    data = {
        "inbox": str(cfg.ingest.inbox) if cfg.ingest.inbox else None,
        "ready": [str(p) for p in ready],
        "warming": [str(p) for p in warming],
        "failures": failures,
        "index": stats,
    }

    def render() -> None:
        console.print(f"取り込み元: {data['inbox']}")
        console.print(f"  処理可能: {len(ready)} 件 / 書き込み中とみなし待機: {len(warming)} 件")
        for p in ready[:10]:
            console.print(f"    - {p.name}")
        if failures:
            console.print(f"[red]失敗: {len(failures)} 件[/red]")
            for f in failures[:5]:
                console.print(f"    - {f['name']}: {(f['error'] or '')[:100]}")
        console.print(f"  index: {stats}")

    _emit(data, as_json, render)


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="検索語")],
    project: Annotated[str | None, typer.Option("--project", "-p")] = None,
    since: Annotated[str | None, typer.Option("--since", help="YYYY-MM-DD 以降")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    config: ConfigOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """台帳を全文検索する。"""
    cfg = Ctx.load(config)
    with State(LedgerPaths(cfg.ledger).state_db) as state:
        rows = [dict(r) for r in state.search(query, project, since, limit)]

    def render() -> None:
        if not rows:
            console.print("[yellow]該当なし[/yellow]")
            return
        for r in rows:
            head = f"[bold]{r['title']}[/bold] ({r['date'][:10]}"
            head += f" / {r['project_id']})" if r["project_id"] else ")"
            console.print(head)
            console.print(f"  {r['snippet']}")
            console.print(f"  [dim]{r['doc']}[/dim]")

    _emit(rows, as_json, render)


@app.command()
def questions(
    project: Annotated[str | None, typer.Option("--project", "-p")] = None,
    config: ConfigOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """未解決の論点を横断で一覧する。"""
    cfg = Ctx.load(config)
    registry = Registry.load(cfg.ledger)
    with State(LedgerPaths(cfg.ledger).state_db) as state:
        rows = [dict(r) for r in state.open_items("question", project)]
    for r in rows:
        r["owner"] = registry.display_person(r["owner"]) if r["owner"] else None

    def render() -> None:
        if not rows:
            console.print("[green]未解決の論点はありません[/green]")
            return
        table = Table("日付", "案件", "論点", "提起")
        for r in rows:
            table.add_row(r["date"][:10], r["project_id"] or "-", r["text"], r["owner"] or "-")
        console.print(table)

    _emit(rows, as_json, render)


@app.command()
def actions(
    project: Annotated[str | None, typer.Option("--project", "-p")] = None,
    config: ConfigOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """未完了のアクションを一覧する。"""
    cfg = Ctx.load(config)
    registry = Registry.load(cfg.ledger)
    with State(LedgerPaths(cfg.ledger).state_db) as state:
        rows = [dict(r) for r in state.open_items("action", project)]
    for r in rows:
        r["owner"] = registry.display_person(r["owner"]) if r["owner"] else None

    def render() -> None:
        if not rows:
            console.print("[green]未完了のアクションはありません[/green]")
            return
        table = Table("日付", "案件", "内容", "担当", "期限")
        for r in rows:
            table.add_row(
                r["date"][:10], r["project_id"] or "-", r["text"], r["owner"] or "未定", r["due"] or "未定"
            )
        console.print(table)

    _emit(rows, as_json, render)


@app.command()
def concerns(
    person: Annotated[str | None, typer.Option("--person", help="人物 id または名前")] = None,
    project: Annotated[str | None, typer.Option("--project", "-p")] = None,
    months: Annotated[int, typer.Option("--months", help="遡る月数")] = 12,
    config: ConfigOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """誰が何を気にしているかを集計する (温度感の蓄積)。"""
    cfg = Ctx.load(config)
    rows = aggregate_concerns(cfg.ledger, person, project, months)

    def render() -> None:
        if not rows:
            console.print("[yellow]記録された温度感がありません[/yellow]")
            return
        table = Table("人", "論点", "回数", "強度", "種類", "関心事")
        for r in rows:
            table.add_row(
                r["who"],
                r["topic"],
                str(r["count"]),
                r["max_intensity"],
                ",".join(r["kinds"]),
                " / ".join(r["concerns"])[:60],
            )
        console.print(table)

    _emit(rows, as_json, render)


@app.command()
def topics(
    show: Annotated[str | None, typer.Option("--show", help="1つの論点を掘り下げる")] = None,
    project: Annotated[str | None, typer.Option("--project", "-p")] = None,
    months: Annotated[int, typer.Option("--months", help="遡る月数")] = 12,
    min_count: Annotated[int, typer.Option("--min-count", help="この回数以上のものだけ")] = 1,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 30,
    config: ConfigOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """会議や案件をまたいで繰り返し出ている論点を集計する。

    案件になる前の話題を見つけるためのコマンド。案件をまたぐものほど上に来ます。
    案件未割当(_inbox)の話題も「まだどこにも属していない」ものとして数えます。
    """
    cfg = Ctx.load(config)

    if show:
        detail = topic_detail(cfg.ledger, show, months)
        if detail is None:
            _fail(f"論点 '{show}' の記録がありません")

        def render_detail() -> None:
            console.print(f"[bold]{detail['topic']}[/bold]")
            reach = "、".join(detail["projects"]) or "(なし)"
            console.print(
                f"  {detail['count']}回 / {detail['meeting_count']}会議 / 案件: {reach}"
                + (f" / 未割当 {detail['unassigned']}件" if detail["unassigned"] else "")
            )
            if detail["people"]:
                console.print(f"  関係者: {'、'.join(detail['people'])}")
            if detail["kinds"]:
                console.print(f"  温度: {'、'.join(detail['kinds'])} (最大 {detail['max_intensity']})")
            if detail["concerns"]:
                console.print("\n  [bold]根底にある関心事[/bold]")
                for c in detail["concerns"]:
                    console.print(f"    - {c}")
            if detail["open_questions"]:
                console.print("\n  [bold]未解決の論点[/bold]")
                for q in detail["open_questions"]:
                    console.print(f"    - {q}")
            console.print("\n  [bold]出現箇所[/bold]")
            for o in detail["occurrences"]:
                where = o["project"] or "[yellow]_inbox[/yellow]"
                console.print(f"    {o['date'][:10]} {where} — {o['segment']}")
                console.print(f"      [dim]{o['doc']}[/dim]")

        _emit(detail, as_json, render_detail)
        return

    rows = aggregate_topics(cfg.ledger, months, project, min_count)[:limit]

    def render() -> None:
        if not rows:
            console.print(
                "[yellow]記録された論点がありません[/yellow]\n"
                "議事録を取り込むと、抽出された論点がここに集計されます。"
            )
            return
        table = Table("論点", "回数", "会議", "案件", "未割当", "温度", "未解決")
        for r in rows:
            topic = f"[bold]{r['topic']}[/bold]" if r["cross_project"] else r["topic"]
            table.add_row(
                topic,
                str(r["count"]),
                str(r["meeting_count"]),
                "、".join(r["projects"]) or "-",
                str(r["unassigned"]) if r["unassigned"] else "-",
                r["max_intensity"] if r["kinds"] else "-",
                str(len(r["open_questions"])) if r["open_questions"] else "-",
            )
        console.print(table)
        console.print(
            "\n[bold]太字[/bold]は複数の案件(または未割当)にまたがる論点です。"
            "個別案件では解けていない可能性があります。"
        )
        console.print("掘り下げ: [bold]comdiary topics --show <論点>[/bold]")

    _emit(rows, as_json, render)


@app.command()
def context(
    project: Annotated[str | None, typer.Argument(help="案件 id")] = None,
    purpose: Annotated[str, typer.Option("--purpose", help=f"用途: {', '.join(PURPOSES)}")] = "general",
    months: Annotated[int, typer.Option("--months")] = 6,
    config: ConfigOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """LLM に渡すコンテキストパックを作る (壁打ち・資料作成の起点)。"""
    cfg = Ctx.load(config)
    if purpose not in PURPOSES:
        _fail(f"未知の purpose: {purpose} (有効: {', '.join(PURPOSES)})")
    pack = build_pack(cfg.ledger, project, purpose, months)
    _emit(pack.to_dict(), as_json, lambda: console.print(pack.to_markdown()))


@app.command()
def show(
    project: Annotated[str, typer.Argument(help="案件 id")],
    section: Annotated[str, typer.Option("--section", "-s", help=f"{', '.join(SECTIONS)}")] = "readme",
    config: ConfigOpt = None,
) -> None:
    """台帳のファイルをそのまま表示する。"""
    cfg = Ctx.load(config)
    paths = LedgerPaths(cfg.ledger)
    try:
        target = paths.section_path(project, section)
    except ValueError as exc:
        _fail(str(exc))
    if target.is_dir():
        files = sorted(target.rglob("*.md"))
        if not files:
            console.print(f"[yellow]{target} は空です[/yellow]")
            return
        for f in files:
            console.print(f"[dim]{f}[/dim]")
        return
    if not target.is_file():
        _fail(f"ありません: {target}")
    console.print(target.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


@app.command()
def append(
    project: Annotated[str, typer.Argument(help="案件 id")],
    text: Annotated[str | None, typer.Option("--text", "-t", help="本文。省略時は標準入力")] = None,
    section: Annotated[str, typer.Option("--section", "-s", help="logs | open-questions | handoff")] = "logs",
    commit: Annotated[bool, typer.Option(help="git コミットする")] = True,
    config: ConfigOpt = None,
) -> None:
    """台帳に手で追記する。"""
    cfg = Ctx.load(config)
    paths = LedgerPaths(cfg.ledger)
    registry = Registry.load(cfg.ledger)
    if not registry.project(project):
        _fail(f"案件 '{project}' が見つかりません")
    body = text if text is not None else sys.stdin.read()
    if not body.strip():
        _fail("追記する本文がありません")

    try:
        target = paths.section_path(project, section)
    except ValueError as exc:
        _fail(str(exc))
    if target.is_dir():
        _fail(f"セクション '{section}' はディレクトリです。--section logs などを指定してください。")

    stamp = now()
    entry = f"\n### {stamp:%Y-%m-%d %H:%M} (手動追記)\n\n{body.strip()}\n"
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    atomic_write(target, existing.rstrip("\n") + "\n" + entry if existing else entry.lstrip("\n"))
    console.print(f"[green]追記しました[/green] {target}")
    if commit and cfg.git.enabled:
        _commit(cfg, f"append: {project}/{section}")


@app.command()
def assign(
    meeting_id: Annotated[str, typer.Argument(help="会議 id")],
    segment_id: Annotated[str, typer.Argument(help="セグメント id (s1 等)")],
    project: Annotated[str, typer.Argument(help="割り当て先の案件 id")],
    commit: Annotated[bool, typer.Option(help="git コミットする")] = True,
    config: ConfigOpt = None,
) -> None:
    """_inbox のセグメントを案件に割り当て直す。"""
    cfg = Ctx.load(config)
    paths = LedgerPaths(cfg.ledger)
    registry = Registry.load(cfg.ledger)
    target_project = registry.project(project)
    if not target_project:
        _fail(f"案件 '{project}' が見つかりません")

    meeting_path = _find_meeting_json(paths, meeting_id)
    if not meeting_path:
        _fail(f"会議 '{meeting_id}' が見つかりません")
    meeting = Meeting.model_validate_json(meeting_path.read_text(encoding="utf-8"))
    segment = next((s for s in meeting.segments if s.segment_id == segment_id), None)
    if not segment:
        _fail(f"セグメント '{segment_id}' が見つかりません")

    old_inbox = (
        paths.inbox
        / f"{meeting.date:%Y-%m}"
        / f"{meeting_path.stem}--{segment_id}.md"
    )
    segment.project_id = project
    segment.match_method = "manual"

    writer = LedgerWriter(paths, registry=registry)
    writer.ensure_project(target_project)
    result = writer.write_meeting(meeting, meeting_path.stem)
    old_inbox.unlink(missing_ok=True)

    with State(paths.state_db) as state:
        state.index_meeting(meeting, str(result.meeting_md), str(result.meeting_json))

    console.print(f"[green]割り当てました[/green] {meeting_id}/{segment_id} → {project}")
    if commit and cfg.git.enabled:
        _commit(cfg, f"assign: {meeting_id}/{segment_id} → {project}")


@app.command()
def triage(config: ConfigOpt = None, as_json: JsonOpt = False) -> None:
    """_inbox に溜まった未割当セグメントを一覧する。"""
    cfg = Ctx.load(config)
    paths = LedgerPaths(cfg.ledger)
    with State(paths.state_db) as state:
        rows = [
            dict(r)
            for r in state.conn.execute(
                "SELECT s.*, m.title AS meeting_title FROM segments s"
                " JOIN meetings m ON m.meeting_id = s.meeting_id"
                " WHERE s.project_id IS NULL ORDER BY s.date DESC"
            ).fetchall()
        ]

    def render() -> None:
        if not rows:
            console.print("[green]未割当のセグメントはありません[/green]")
            return
        table = Table("日付", "会議", "セグメント", "タイトル", "概要")
        for r in rows:
            table.add_row(
                r["date"][:10],
                r["meeting_title"][:20],
                f"{r['meeting_id']} {r['segment_id']}",
                r["title"][:30],
                r["summary"][:40],
            )
        console.print(table)
        console.print("\n割り当て: [bold]comdiary assign <meeting_id> <segment_id> <project_id>[/bold]")

    _emit(rows, as_json, render)


# ---------------------------------------------------------------------------
# maintenance
# ---------------------------------------------------------------------------


@app.command()
def reindex(config: ConfigOpt = None) -> None:
    """meetings/*.json から索引を作り直す (DB は捨てても復元できる)。"""
    cfg = Ctx.load(config)
    paths = LedgerPaths(cfg.ledger)
    files = sorted(paths.meetings.rglob("*.json"))
    with State(paths.state_db) as state:
        for path in files:
            meeting = Meeting.model_validate_json(path.read_text(encoding="utf-8"))
            state.index_meeting(meeting, str(path.with_suffix(".md")), str(path))
    console.print(f"[green]再索引しました[/green] {len(files)} 件")


@app.command()
def rerender(
    commit: Annotated[bool, typer.Option(help="git コミットする")] = True,
    config: ConfigOpt = None,
) -> None:
    """meetings/*.json から markdown を作り直す (テンプレート変更後に使う)。"""
    cfg = Ctx.load(config)
    paths = LedgerPaths(cfg.ledger)
    registry = Registry.load(cfg.ledger)
    writer = LedgerWriter(paths, registry=registry)
    changed = 0
    for path in sorted(paths.meetings.rglob("*.json")):
        meeting = Meeting.model_validate_json(path.read_text(encoding="utf-8"))
        for pid in meeting.project_ids():
            project = registry.project(pid)
            if project:
                writer.ensure_project(project)
        result = writer.write_meeting(meeting, path.stem)
        changed += len(result.changed)
    console.print(f"[green]再生成しました[/green] 変更 {changed} ファイル")
    if commit and changed and cfg.git.enabled:
        _commit(cfg, "rerender: テンプレートから再生成")


@app.command("mcp")
def mcp_serve(config: ConfigOpt = None) -> None:
    """MCP サーバを stdio で起動する。"""
    from .mcp_server import serve

    serve(Ctx.load(config))


# ---------------------------------------------------------------------------


def _find_meeting_json(paths: LedgerPaths, meeting_id: str) -> Path | None:
    for path in paths.meetings.rglob("*.json"):
        if meeting_id in path.stem:
            return path
        try:
            if f'"meeting_id": "{meeting_id}"' in path.read_text(encoding="utf-8")[:400]:
                return path
        except OSError:
            continue
    return None


def _commit(cfg: Config, message: str) -> None:
    try:
        result = gitops.commit(cfg.ledger, message, forbid_remote=cfg.git.forbid_remote)
        console.print(f"[dim]git: {result.message}[/dim]")
    except gitops.RemoteConfigured as exc:
        err.print(f"[red]コミットを中止しました[/red]\n{exc}")
    except gitops.GitUnavailable:
        pass


def main() -> None:
    app()


if __name__ == "__main__":
    main()
