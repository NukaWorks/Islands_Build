"""
Git helpers for the Islands mono-repo.

All functions operate on the list of repository roots defined in
``config.REPOS`` (falls back to ``config.PROJECTS`` directory list when the
attribute is absent so older configs keep working).

Public API
----------
  is_git_repo(path)           → bool
  current_branch(path)        → str | None
  status(path)                → dict
  fetch_all(path)             → bool
  checkout(path, branch)      → bool
  create_branch(path, branch) → bool
  pull(path)                  → bool
  list_branches(path, remote) → list[str]
  print_status_table(repos)   → None
  print_branches_table(repos) → None
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import logger as log

# ── git executable ─────────────────────────────────────────────────────────

def _git() -> Optional[str]:
    """Return the path to git, or None if not found."""
    return shutil.which("git")


def _run(args: list[str], cwd: Path, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a git sub-command, return the CompletedProcess."""
    git = _git()
    if git is None:
        raise RuntimeError("git executable not found on PATH.")
    cmd = [git] + args
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=capture,
        text=True,
    )


# ── Low-level primitives ───────────────────────────────────────────────────

def is_git_repo(path: Path) -> bool:
    """Return True if *path* is inside (or is) a git work-tree."""
    try:
        r = _run(["rev-parse", "--is-inside-work-tree"], cwd=path)
        return r.returncode == 0 and r.stdout.strip() == "true"
    except (RuntimeError, FileNotFoundError):
        return False


def current_branch(path: Path) -> Optional[str]:
    """Return the name of the currently checked-out branch, or None."""
    try:
        r = _run(["symbolic-ref", "--short", "HEAD"], cwd=path)
        if r.returncode == 0:
            return r.stdout.strip()
        # detached HEAD
        r2 = _run(["rev-parse", "--short", "HEAD"], cwd=path)
        if r2.returncode == 0:
            return f"(detached {r2.stdout.strip()})"
        return None
    except RuntimeError:
        return None


def status(path: Path) -> dict:
    """
    Return a status dict for the repo at *path*::

        {
          "branch":    str | None,
          "ahead":     int,          # commits ahead of upstream
          "behind":    int,          # commits behind upstream
          "staged":    int,          # files staged
          "unstaged":  int,          # tracked files with unstaged changes
          "untracked": int,          # untracked files
          "clean":     bool,
        }
    """
    result = {
        "branch":    current_branch(path),
        "ahead":     0,
        "behind":    0,
        "staged":    0,
        "unstaged":  0,
        "untracked": 0,
        "clean":     True,
    }
    try:
        # porcelain v2 gives us structured, stable output
        r = _run(["status", "--porcelain=v2", "--branch"], cwd=path)
        if r.returncode != 0:
            return result

        for line in r.stdout.splitlines():
            if line.startswith("# branch.ab "):
                parts = line.split()
                # format: # branch.ab +N -N
                for p in parts:
                    if p.startswith("+"):
                        result["ahead"] = int(p[1:])
                    elif p.startswith("-"):
                        result["behind"] = int(p[1:])
            elif line.startswith("1 ") or line.startswith("2 "):
                xy = line[2:4]
                if xy[0] != ".":
                    result["staged"] += 1
                if xy[1] != ".":
                    result["unstaged"] += 1
            elif line.startswith("? "):
                result["untracked"] += 1

        result["clean"] = (
            result["staged"] == 0
            and result["unstaged"] == 0
            and result["untracked"] == 0
        )
    except RuntimeError:
        pass
    return result


def list_branches(path: Path, *, remote: bool = False) -> list[str]:
    """Return sorted list of local (or remote) branch names."""
    args = ["branch", "--format=%(refname:short)"]
    if remote:
        args.append("-r")
    try:
        r = _run(args, cwd=path)
        if r.returncode != 0:
            return []
        return sorted(
            b.strip() for b in r.stdout.splitlines() if b.strip()
        )
    except RuntimeError:
        return []


def fetch_all(path: Path, *, verbose: bool = False) -> bool:
    """Run ``git fetch --all --prune`` for the repo at *path*."""
    try:
        r = _run(["fetch", "--all", "--prune"], cwd=path, capture=not verbose)
        return r.returncode == 0
    except RuntimeError as exc:
        log.error(str(exc))
        return False


def checkout(path: Path, branch: str, *, create: bool = False) -> bool:
    """
    Checkout *branch* in the repo at *path*.
    If *create* is True, pass ``-b`` to create the branch.
    """
    args = ["checkout"]
    if create:
        args.append("-b")
    args.append(branch)
    try:
        r = _run(args, cwd=path, capture=False)
        return r.returncode == 0
    except RuntimeError as exc:
        log.error(str(exc))
        return False


def create_branch(path: Path, branch: str) -> bool:
    """Create a new branch at HEAD without switching to it."""
    try:
        r = _run(["branch", branch], cwd=path, capture=False)
        return r.returncode == 0
    except RuntimeError as exc:
        log.error(str(exc))
        return False


def pull(path: Path, *, verbose: bool = False) -> bool:
    """Run ``git pull`` for the repo at *path*."""
    try:
        r = _run(["pull"], cwd=path, capture=not verbose)
        return r.returncode == 0
    except RuntimeError as exc:
        log.error(str(exc))
        return False


# ── Pretty-print helpers ───────────────────────────────────────────────────

def _status_symbol(st: dict) -> str:
    """Return a compact, coloured status indicator string."""
    if st.get("clean"):
        return "[green]✔ clean[/green]"
    parts = []
    if st["staged"]:
        parts.append(f"[yellow]{st['staged']} staged[/yellow]")
    if st["unstaged"]:
        parts.append(f"[yellow]{st['unstaged']} modified[/yellow]")
    if st["untracked"]:
        parts.append(f"[dim]{st['untracked']} untracked[/dim]")
    return "  ".join(parts) if parts else "[dim]unknown[/dim]"


def _ahead_behind(st: dict) -> str:
    ahead  = st.get("ahead",  0)
    behind = st.get("behind", 0)
    if ahead == 0 and behind == 0:
        return "[dim]up-to-date[/dim]"
    tokens = []
    if ahead:
        tokens.append(f"[cyan]↑{ahead}[/cyan]")
    if behind:
        tokens.append(f"[red]↓{behind}[/red]")
    return " ".join(tokens)


def print_status_table(repos: list[dict]) -> None:
    """
    Print a rich (or plain-text) table with branch / status for every repo.

    Each element of *repos* must have ``"name"`` and ``"dir"`` keys.
    """
    rows = []
    for repo in repos:
        path = Path(repo["dir"])
        name = repo["name"]
        if not is_git_repo(path):
            rows.append((name, "—", "[dim]not a git repo[/dim]", "—"))
            continue
        st = status(path)
        branch = st["branch"] or "[dim]unknown[/dim]"
        rows.append((name, branch, _status_symbol(st), _ahead_behind(st)))

    try:
        from rich.table import Table
        from rich.console import Console
        table = Table(title="Git Status", show_lines=True)
        table.add_column("Repo",          style="bold cyan", no_wrap=True)
        table.add_column("Branch",        style="bold")
        table.add_column("Working Tree",  justify="left")
        table.add_column("Upstream",      justify="left")
        for row in rows:
            table.add_row(*row)
        Console().print(table)
    except ImportError:
        print(f"\n{'Repo':<16}  {'Branch':<20}  {'Working Tree':<30}  Upstream")
        print("─" * 85)
        for name, branch, wt, upstream in rows:
            # strip rich markup for plain output
            plain = lambda s: re.sub(r"\[/?[^]]+]", "", s)
            print(f"{name:<16}  {plain(branch):<20}  {plain(wt):<30}  {plain(upstream)}")
        print()


def print_branches_table(repos: list[dict]) -> None:
    """Print local branches for every repo."""
    try:
        from rich.table import Table
        from rich.console import Console
        table = Table(title="Git Branches", show_lines=True)
        table.add_column("Repo",   style="bold cyan", no_wrap=True)
        table.add_column("Current Branch", style="bold green")
        table.add_column("All Local Branches", style="dim")
        for repo in repos:
            path = Path(repo["dir"])
            name = repo["name"]
            if not is_git_repo(path):
                table.add_row(name, "—", "[dim]not a git repo[/dim]")
                continue
            cur    = current_branch(path) or "?"
            branches = list_branches(path)
            others = [b for b in branches if b != cur]
            branch_list = ("  ".join(others)) if others else "[dim](none)[/dim]"
            table.add_row(name, cur, branch_list)
        Console().print(table)
    except ImportError:
        plain = lambda s: re.sub(r"\[/?[^]]+]", "", s)
        print(f"\n{'Repo':<16}  {'Current':<20}  All Local Branches")
        print("─" * 80)
        for repo in repos:
            path = Path(repo["dir"])
            name = repo["name"]
            if not is_git_repo(path):
                print(f"{name:<16}  {'—':<20}  not a git repo")
                continue
            cur = current_branch(path) or "?"
            branches = list_branches(path)
            print(f"{name:<16}  {cur:<20}  {',  '.join(branches)}")
        print()




