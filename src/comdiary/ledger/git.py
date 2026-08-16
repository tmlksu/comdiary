"""Git operations on the ledger.

The ledger holds company data and is meant to stay on one machine, so this
module actively refuses to help it reach a remote: `ensure_no_remote` is
checked before every commit and by `comdiary doctor`.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitUnavailable(RuntimeError):
    pass


class RemoteConfigured(RuntimeError):
    pass


@dataclass
class GitResult:
    ok: bool
    message: str


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    if shutil.which("git") is None:
        raise GitUnavailable("git が見つかりません")
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def is_repo(root: Path) -> bool:
    return (root / ".git").exists()


def init(root: Path) -> GitResult:
    if is_repo(root):
        return GitResult(True, "既に git リポジトリです")
    proc = _git(root, "init", "-b", "main")
    return GitResult(proc.returncode == 0, proc.stdout or proc.stderr)


def remotes(root: Path) -> list[str]:
    if not is_repo(root):
        return []
    proc = _git(root, "remote")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def ensure_no_remote(root: Path) -> None:
    found = remotes(root)
    if found:
        raise RemoteConfigured(
            f"台帳リポジトリにリモートが設定されています: {', '.join(found)}。\n"
            "この台帳は社内情報を含むためローカル専用です。意図的な場合は "
            "comdiary.toml の [git] forbid_remote = false を設定してください。"
        )


def commit(root: Path, message: str, forbid_remote: bool = True) -> GitResult:
    if not is_repo(root):
        return GitResult(False, "git リポジトリではないためコミットしません")
    if forbid_remote:
        ensure_no_remote(root)
    add = _git(root, "add", "-A")
    if add.returncode != 0:
        return GitResult(False, add.stderr)
    status = _git(root, "status", "--porcelain")
    if not status.stdout.strip():
        return GitResult(True, "変更なし")
    proc = _git(root, "commit", "-m", message)
    return GitResult(proc.returncode == 0, (proc.stdout or proc.stderr).strip())
