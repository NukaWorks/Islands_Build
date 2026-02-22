"""
Google ``repo`` tool helpers for the Islands mono-repo.

Wraps the ``repo`` CLI and the manifest XML so the build system can:
  • query manifest projects and their configured revisions
  • run repo sync / status / info
  • run arbitrary commands across all projects via ``repo forall``
  • switch the manifest revision (branch) for one or all projects
  • add / remove projects in the manifest
  • show a rich status / info table

The manifest is expected at  <workspace>/.repo/manifests/default.xml
(the standard repo layout).  All path arguments are absolute ``Path``s
or strings; the workspace root is read from ``config.WORKSPACE``.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional
from xml.dom import minidom

import logger as log

# ── repo executable ────────────────────────────────────────────────────────

def _repo_bin() -> Optional[str]:
    return shutil.which("repo")


def is_available() -> bool:
    """Return True if the ``repo`` tool is on PATH."""
    return _repo_bin() is not None


def _run(
    args: list[str],
    cwd: Path,
    *,
    capture: bool = True,
    check: bool = False,
) -> subprocess.CompletedProcess:
    exe = _repo_bin()
    if exe is None:
        raise RuntimeError(
            "Google repo tool not found on PATH.\n"
            "Install it:  https://android.googlesource.com/tools/repo"
        )
    return subprocess.run(
        [exe] + args,
        cwd=str(cwd),
        capture_output=capture,
        text=True,
        check=check,
    )


# ── Manifest helpers ───────────────────────────────────────────────────────

class Manifest:
    """
    Thin wrapper around a repo ``default.xml`` manifest file.

    Attributes
    ----------
    path : Path
        Absolute path to the manifest XML file.
    tree : ET.ElementTree
        Parsed XML tree (mutate then call ``save()``).
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.path = workspace / ".repo" / "manifests" / "default.xml"
        if not self.path.exists():
            raise FileNotFoundError(
                f"Manifest not found: {self.path}\n"
                "Is this workspace initialised with 'repo init'?"
            )
        self.tree = ET.parse(str(self.path))

    @property
    def root(self) -> ET.Element:
        return self.tree.getroot()

    # ── read helpers ──────────────────────────────────────────────────────

    def default_revision(self) -> Optional[str]:
        """Return the <default revision="…"> attribute, or None."""
        d = self.root.find("default")
        return d.get("revision") if d is not None else None

    def default_remote(self) -> Optional[str]:
        d = self.root.find("default")
        return d.get("remote") if d is not None else None

    def remotes(self) -> dict[str, str]:
        """Return {name: fetch-url} for every <remote> element."""
        return {
            r.get("name", ""): r.get("fetch", "")
            for r in self.root.findall("remote")
        }

    def projects(self) -> list[dict]:
        """
        Return a list of dicts, one per <project> element::

            {
              "name":     str,   # GitHub repo name
              "path":     str,   # relative checkout path
              "revision": str,   # branch/tag/SHA (falls back to <default>)
              "remote":   str,   # remote name (falls back to <default>)
              "groups":   str,   # groups attribute or ""
            }
        """
        default_rev    = self.default_revision() or "master"
        default_remote = self.default_remote() or ""
        result = []
        for p in self.root.findall("project"):
            result.append({
                "name":     p.get("name", ""),
                "path":     p.get("path", p.get("name", "")),
                "revision": p.get("revision", default_rev),
                "remote":   p.get("remote",   default_remote),
                "groups":   p.get("groups",   ""),
            })
        return result

    def get_project(self, name_or_path: str) -> Optional[ET.Element]:
        """Return the <project> Element matching name or path, or None."""
        for p in self.root.findall("project"):
            if p.get("name") == name_or_path or p.get("path") == name_or_path:
                return p
        return None

    # ── write helpers ─────────────────────────────────────────────────────

    def set_default_revision(self, revision: str) -> None:
        """Change the <default revision="…"> attribute."""
        d = self.root.find("default")
        if d is None:
            raise RuntimeError("<default> element not found in manifest.")
        d.set("revision", revision)

    def set_project_revision(self, name_or_path: str, revision: str) -> bool:
        """
        Set (or add) the ``revision`` attribute on a specific <project>.
        Returns False if the project was not found.
        """
        p = self.get_project(name_or_path)
        if p is None:
            return False
        p.set("revision", revision)
        return True

    def clear_project_revision(self, name_or_path: str) -> bool:
        """
        Remove the per-project ``revision`` override so it inherits <default>.
        Returns False if the project was not found.
        """
        p = self.get_project(name_or_path)
        if p is None:
            return False
        if "revision" in p.attrib:
            del p.attrib["revision"]
        return True

    def add_project(
        self,
        name: str,
        path: str,
        *,
        revision: Optional[str] = None,
        remote: Optional[str] = None,
        groups: Optional[str] = None,
    ) -> bool:
        """
        Add a new <project> element. Returns False if it already exists.
        """
        if self.get_project(name) or self.get_project(path):
            return False
        attribs: dict[str, str] = {"path": path, "name": name}
        if revision:
            attribs["revision"] = revision
        if remote:
            attribs["remote"] = remote
        if groups:
            attribs["groups"] = groups
        ET.SubElement(self.root, "project", **attribs)
        return True

    def remove_project(self, name_or_path: str) -> bool:
        """Remove a <project> element by name or path. Returns False if not found."""
        p = self.get_project(name_or_path)
        if p is None:
            return False
        self.root.remove(p)
        return True

    def save(self, path: Optional[Path] = None) -> None:
        """Write the (possibly modified) manifest back to disk."""
        dest = path or self.path
        raw  = ET.tostring(self.root, encoding="unicode")
        dom  = minidom.parseString(raw)
        pretty = "\n".join(dom.toprettyxml(indent="  ").splitlines()) + "\n"
        dest.write_text(pretty, encoding="utf-8")

    def as_text(self) -> str:
        """Return the current manifest as a pretty-printed XML string."""
        raw  = ET.tostring(self.root, encoding="unicode")
        dom  = minidom.parseString(raw)
        return "\n".join(dom.toprettyxml(indent="  ").splitlines()) + "\n"


# ── repo command wrappers ──────────────────────────────────────────────────

def sync(
    workspace: Path,
    *,
    projects: Optional[list[str]] = None,
    jobs: int = 4,
    verbose: bool = False,
) -> bool:
    """
    Run ``repo sync``.

    Parameters
    ----------
    projects
        Limit sync to these project paths/names (empty = all).
    jobs
        Parallel fetch jobs (``-j`` flag).
    """
    args = ["sync", f"-j{jobs}", "--no-tags"]
    if not verbose:
        args.append("-q")
    if projects:
        args.extend(projects)
    try:
        r = _run(args, cwd=workspace, capture=not verbose)
        return r.returncode == 0
    except RuntimeError as exc:
        log.error(str(exc))
        return False


def repo_status(workspace: Path) -> str:
    """Return the raw text output of ``repo status``."""
    try:
        r = _run(["status"], cwd=workspace)
        return r.stdout
    except RuntimeError as exc:
        log.error(str(exc))
        return ""


def repo_info(workspace: Path) -> str:
    """Return the raw text output of ``repo info``."""
    try:
        r = _run(["info"], cwd=workspace)
        return r.stdout
    except RuntimeError as exc:
        log.error(str(exc))
        return ""


def forall(workspace: Path, command: str, *, verbose: bool = False) -> bool:
    """
    Run ``repo forall -c <command>`` across all projects.
    Streams output to the terminal regardless of *verbose*.
    """
    try:
        r = _run(["forall", "-c", command], cwd=workspace, capture=False)
        return r.returncode == 0
    except RuntimeError as exc:
        log.error(str(exc))
        return False


def list_projects(workspace: Path) -> list[dict]:
    """
    Return projects via ``repo list`` as ``[{"path": …, "name": …}]``.
    Falls back to manifest parsing if ``repo list`` fails.
    """
    try:
        r = _run(["list"], cwd=workspace)
        if r.returncode != 0:
            raise RuntimeError("repo list failed")
        projects = []
        for line in r.stdout.splitlines():
            # format:  "path : name"
            if " : " in line:
                path, name = line.split(" : ", 1)
                projects.append({"path": path.strip(), "name": name.strip()})
        return projects
    except RuntimeError:
        try:
            m = Manifest(workspace)
            return [{"path": p["path"], "name": p["name"]} for p in m.projects()]
        except FileNotFoundError:
            return []


def checkout_branch(
    workspace: Path,
    branch: str,
    *,
    create: bool = False,
    force: bool = False,
) -> bool:
    """
    Switch every project in the repo manifest to *branch* using
    ``repo forall -c git checkout [−b] <branch>``.

    When *create* is True, a new branch is created in repos that don't
    have it yet; repos that already have it are simply checked out.
    When *force* is False the function stops on the first failure.
    """
    log.info(f"repo: checking out '{branch}' across all projects…")

    projects = list_projects(workspace)
    failed: list[str] = []

    for proj in projects:
        proj_path = workspace / proj["path"]
        name      = proj["name"]

        # check if branch already exists locally
        r = subprocess.run(
            ["git", "branch", "--list", branch],
            cwd=str(proj_path),
            capture_output=True,
            text=True,
        )
        exists = bool(r.stdout.strip())

        git_args = ["git", "checkout"]
        if create and not exists:
            git_args.append("-b")
        git_args.append(branch)

        rc = subprocess.run(git_args, cwd=str(proj_path), capture_output=False, text=True)
        if rc.returncode != 0:
            log.error(f"{name}: checkout failed")
            failed.append(name)
            if not force:
                return False
        else:
            log.success(f"{name}: ✔ on '{branch}'")

    if failed:
        log.error(f"Checkout failed for: {', '.join(failed)}")
        return False
    return True


# ── Pretty-print helpers ───────────────────────────────────────────────────

def print_manifest_table(workspace: Path) -> None:
    """Print a rich/plain table of all manifest projects and their revisions."""
    try:
        m = Manifest(workspace)
        projects = m.projects()
        default_rev = m.default_revision() or "master"
    except FileNotFoundError as exc:
        log.error(str(exc))
        return

    try:
        from rich.table import Table
        from rich.console import Console
        table = Table(title="Manifest Projects", show_lines=True)
        table.add_column("Path",     style="bold cyan",  no_wrap=True)
        table.add_column("Name",     style="dim")
        table.add_column("Revision", style="bold yellow")
        table.add_column("Remote",   style="dim")
        for p in projects:
            rev = p["revision"]
            rev_str = (
                f"[green]{rev}[/green]"
                if rev == default_rev
                else f"[magenta]{rev}[/magenta]"
            )
            table.add_row(p["path"], p["name"], rev_str, p["remote"])
        Console().print(table)
        Console().print(
            f"[dim]Default revision:[/dim] [bold]{default_rev}[/bold]  "
            f"[dim]Default remote:[/dim] [bold]{m.default_remote()}[/bold]  "
            f"[dim]Manifest:[/dim] {m.path}"
        )
    except ImportError:
        plain = lambda s: re.sub(r"\[/?[^]]+]", "", s)
        print(f"\n{'Path':<20}  {'Name':<20}  {'Revision':<20}  Remote")
        print("─" * 80)
        for p in projects:
            print(f"{p['path']:<20}  {p['name']:<20}  {p['revision']:<20}  {p['remote']}")
        print(f"\nDefault revision: {default_rev}  |  Manifest: {m.path}\n")


def print_repo_status(workspace: Path) -> None:
    """Stream ``repo status`` output to stdout."""
    out = repo_status(workspace)
    if out:
        print(out)
    else:
        log.warn("No repo status output (is the workspace initialised?)")


def print_repo_info(workspace: Path) -> None:
    """Stream ``repo info`` output to stdout."""
    out = repo_info(workspace)
    if out:
        print(out)
    else:
        log.warn("No repo info output.")

