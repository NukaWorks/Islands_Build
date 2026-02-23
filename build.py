#!/usr/bin/env python3
"""
Islands Build CLI
=================

Usage examples
--------------
  python build.py build-all                          # build every project (tests skipped)
  python build.py build-all --with-tests             # build + run tests for every project
  python build.py build-all --java-version 24.0.2-tem
  python build.py build-all --mode local             # strip GPG plugin (default on dev machines)
  python build.py build-all --mode devel             # strip GPG + append -nightly_<sha> version
  python build.py build-all --mode release           # full pom (GPG signing on)
  python build.py run-islands                        # build all + assemble + launch
  python build.py run-islands --fast-build           # skip build, assemble + launch existing artifacts
  python build.py run-islands --clean                # mvn clean install + wipe output dir before launch
  python build.py run-islands --with-tests --clean --verbose
  python build.py run-islands --java-version 24.0.2-tem
  python build.py assemble                           # only assemble output dir (no build)
  python build.py clean                              # wipe the output dir
  python build.py status                             # show project/artifact status
  python build.py info                               # show resolved paths + workspace projects
  python build.py idea                               # generate IntelliJ IDEA project files
  python build.py idea --force                       # overwrite existing .iml files
  python build.py idea --java-version 24.0.2-tem     # target a specific JDK in IDEA
  python build.py sdk list                           # list installed Java candidates
  python build.py sdk install 24.0.2-tem             # install a Java candidate
  python build.py sdk use 24.0.2-tem                 # switch default Java candidate
  python build.py git status                         # branch + working-tree status for all repos
  python build.py git branches                       # list all local branches for all repos
  python build.py git checkout main                  # switch every repo to 'main'
  python build.py git checkout feature/x --create   # create + switch branch in every repo
  python build.py git fetch                          # git fetch --all --prune on every repo
  python build.py git pull                           # git pull on every repo
  python build.py repo manifest                      # show manifest projects + revisions
  python build.py repo status                        # repo status across all projects
  python build.py repo info                          # repo info (manifest branch, remotes…)
  python build.py repo sync                          # repo sync (fetch + update all projects)
  python build.py repo forall "git log --oneline -3" # run a command in every project
  python build.py repo checkout main                 # switch all projects to 'main'
  python build.py repo checkout feature/x --create  # create + switch in all projects
  python build.py repo manifest set-revision main    # change <default revision> in manifest
  python build.py repo manifest set-revision develop --project ModularKit
  python build.py repo manifest add MyLib MyLib      # add a project to the manifest
  python build.py repo manifest remove MyLib         # remove a project from the manifest
  python build.py project list                       # list all workspace projects
  python build.py project show ModularKit            # print project.json for a project
  python build.py project init /path/to/MyProject    # create a new project.json
  python build.py project set ModularKit version 2.0.0
  python build.py project add-dep ModularKit works.nuka ModularKit
  python build.py project remove-dep Islands UiKit
  python build.py project run ModularKit             # dry-run pre_build hooks
  python build.py project run ModularKit --mode devel
"""

import argparse
import json
import os
import sys
import time
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.dom import minidom

# ── make sure local modules are importable when run as a script ──────────────
sys.path.insert(0, os.path.dirname(__file__))

import config as cfg
import fs
import git as gitutil
import hooks as hooksmod
import logger as log
import maven
import repotool
import runner
import sdkman


# ─────────────────────────────────────────────────────────────────────────────
# Sub-command implementations
# ─────────────────────────────────────────────────────────────────────────────

def _universal_hooks() -> dict:
    """Return the standard pre/post hook table (universal hook for every project)."""
    return {"pre_build": [hooksmod.universal_prebuild], "post_build": []}


def cmd_build_all(args: argparse.Namespace) -> int:
    """Build every project in dependency order."""
    skip_tests = not args.with_tests
    java_ver   = args.java_version or cfg.JAVA_VERSION
    mode       = getattr(args, "mode", None) or cfg.BUILD_MODE
    projects   = cfg.get_projects()
    log.banner(
        "Build All",
        f"Projects: {len(projects)}  |  "
        f"Tests: {'enabled' if args.with_tests else 'skipped'}  |  "
        f"Java: {java_ver or 'ambient'}  |  Mode: {mode}  |  Verbose: {args.verbose}",
    )
    # Resolve env once so we fail early if Java is missing
    env = runner._resolve_env(java_ver)
    if env is None and java_ver:
        return 1

    total = len(projects)
    start = time.time()
    for i, project in enumerate(projects, 1):
        log.step(i, total, project["name"])

        # ── pre-build hooks ──────────────────────────────────────────────
        ctx = hooksmod.build_hook_context(project, mode=mode, verbose=args.verbose,
                                          workspace_dir=cfg.WORKSPACE)
        hook_table = _universal_hooks()
        ok, pom_override, extra_mvn_args = hooksmod.run_hooks("pre_build", hook_table.get("pre_build", []), ctx)
        if not ok:
            log.error(f"Pre-build hook failed for: {project['name']}")
            return 1

        # ── maven build ──────────────────────────────────────────────────
        ok = maven.build_project(
            project["name"],
            project["dir"],
            skip_tests=skip_tests,
            clean=getattr(args, "clean", False),
            verbose=args.verbose,
            env=env,
            pom_override=pom_override,
            extra_maven_args=extra_mvn_args,
        )
        if not ok:
            log.error(f"Build failed at: {project['name']}")
            return 1

        # ── post-build hooks ─────────────────────────────────────────────
        ok, _, _ = hooksmod.run_hooks("post_build", hook_table.get("post_build", []), ctx)
        if not ok:
            log.error(f"Post-build hook failed for: {project['name']}")
            return 1

    log.success(f"All {total} projects built in {log.duration(time.time() - start)}.")
    return 0


def cmd_run_islands(args: argparse.Namespace) -> int:
    """Build all projects then launch Islands via CoffeeLoader."""
    ok = runner.build_and_run_islands(
        skip_tests=not args.with_tests,
        clean_output=args.clean,
        fast_build=args.fast_build,
        clean=args.clean,
        verbose=args.verbose,
        java_opts=args.java_opts,
        java_version=args.java_version,
        mode=getattr(args, "mode", None) or cfg.BUILD_MODE,
    )
    return 0 if ok else 1


def cmd_assemble(args: argparse.Namespace) -> int:
    """Assemble the output directory without rebuilding (expects artifacts exist)."""
    log.banner("Assemble Output")
    ok = runner._assemble_output(clean=args.clean)
    return 0 if ok else 1


def cmd_clean(args: argparse.Namespace) -> int:
    """Wipe the output directory."""
    log.banner("Clean Output")
    fs.clean_output(cfg.OUTPUT_DIR)
    log.success(f"Output directory cleaned: {cfg.OUTPUT_DIR}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Show whether each project's artifact exists."""
    log.banner("Project Status")
    projects = cfg.get_projects()

    rows = []
    for p in projects:
        art = p.get("artifact")
        if art:
            exists = Path(art).exists()
            mark = "[green]✔[/green]" if exists else "[red]✖[/red]"
            rows.append((p["name"], str(Path(art).name), mark, str(Path(art).parent)))
        else:
            rows.append((p["name"], "—", "[dim]?[/dim]", "—"))

    try:
        from rich.table import Table
        from rich.console import Console
        table = Table(title="Maven Artifacts", show_lines=True)
        table.add_column("Project",  style="bold cyan",  no_wrap=True)
        table.add_column("Artifact", style="dim")
        table.add_column("Built",    justify="center")
        table.add_column("Location", style="dim", overflow="fold")
        for name, artifact, mark, location in rows:
            table.add_row(name, artifact, mark, location)
        Console().print(table)
    except ImportError:
        print(f"\n{'Project':<16}  {'Artifact':<45}  Built")
        print("─" * 80)
        for name, artifact, mark, location in rows:
            tick = "✔" if "green" in mark else "✖"
            print(f"{name:<16}  {artifact:<45}  {tick}")
        print()

    if cfg.OUTPUT_DIR.exists():
        jars = list(cfg.OUTPUT_DIR.rglob("*.jar"))
        log.info(f"Output dir: {cfg.OUTPUT_DIR}  ({len(jars)} jar(s))")
    else:
        log.warn(f"Output dir does not exist yet: {cfg.OUTPUT_DIR}")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    """Print resolved paths and workspace project list."""
    log.banner("Build Configuration")

    # Java / sdkman info
    java_ver = cfg.JAVA_VERSION or "ambient (not configured)"
    log.info(f"   {'Configured Java':<22} {java_ver}")
    log.info(f"   {'Auto-install Java':<22} {cfg.AUTO_INSTALL_JAVA}")
    if sdkman.is_available():
        current = sdkman.current_candidate()
        cur_str = f"{current[0]}  ({current[1]})" if current else "none"
        log.info(f"   {'sdkman current Java':<22} {cur_str}")
    else:
        log.warn("   sdkman not found on this machine.")
    print()

    # Static paths
    static_paths = {
        "Workspace":   cfg.WORKSPACE,
        "Build dir":   cfg.BUILD_DIR,
        "Output dir":  cfg.OUTPUT_DIR,
        "Modules dir": cfg.MODULES_DIR,
    }
    for label, path in static_paths.items():
        exists = "✔" if path.exists() else "✖"
        log.info(f"{exists}  {label:<22} {path}")
    print()

    # Discovered projects
    projects = cfg.get_projects()
    log.info(f"Discovered projects ({len(projects)}):")
    for p in projects:
        d = Path(p["dir"])
        art = Path(p["artifact"]) if p.get("artifact") else None
        art_mark = "✔" if (art and art.exists()) else "✖"
        log.info(f"  {art_mark}  {p['name']:<16} {d}")
        if art:
            log.info(f"       {'artifact':<16} {art.name}")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# git sub-commands
# ─────────────────────────────────────────────────────────────────────────────

def _repos() -> list:
    """Return the configured list of git repos (discovered dynamically)."""
    return cfg.get_repos()


def cmd_git_status(args: argparse.Namespace) -> int:
    """Show branch + working-tree status for every repo."""
    log.banner("Git Status", "Branch and working-tree summary for all repos")
    gitutil.print_status_table(_repos())
    return 0


def cmd_git_branches(args: argparse.Namespace) -> int:
    """List all local branches for every repo."""
    log.banner("Git Branches", "Local branches for all repos")
    gitutil.print_branches_table(_repos())
    return 0


def cmd_git_checkout(args: argparse.Namespace) -> int:
    """Switch every repo to *branch* (skip repos that don't have it unless --create)."""
    branch  = args.branch
    create  = args.create
    force   = args.force
    repos   = _repos()

    log.banner(
        "Git Checkout",
        f"branch: {branch}  |  create: {create}  |  repos: {len(repos)}",
    )

    ok_count = skip_count = fail_count = 0
    for repo in repos:
        from pathlib import Path
        path = Path(repo["dir"])
        name = repo["name"]

        if not gitutil.is_git_repo(path):
            log.warn(f"{name}: not a git repo – skipping")
            skip_count += 1
            continue

        available = gitutil.list_branches(path)
        if branch in available or create:
            log.info(f"{name}: checking out '{branch}'…")
            ok = gitutil.checkout(path, branch, create=create and branch not in available)
            if ok:
                log.success(f"{name}: ✔ on '{branch}'")
                ok_count += 1
            else:
                log.error(f"{name}: checkout failed")
                fail_count += 1
                if not force:
                    return 1
        else:
            log.warn(f"{name}: branch '{branch}' not found locally – skipping "
                     "(pass --create to create it)")
            skip_count += 1

    log.info(f"Done – checked out: {ok_count}  skipped: {skip_count}  failed: {fail_count}")
    return 0 if fail_count == 0 else 1


def cmd_git_fetch(args: argparse.Namespace) -> int:
    """Run ``git fetch --all --prune`` on every repo."""
    log.banner("Git Fetch", "Fetching remotes for all repos")
    repos = _repos()
    failed = []
    for repo in repos:
        from pathlib import Path
        path = Path(repo["dir"])
        name = repo["name"]
        if not gitutil.is_git_repo(path):
            log.warn(f"{name}: not a git repo – skipping")
            continue
        log.info(f"{name}: fetching…")
        ok = gitutil.fetch_all(path, verbose=args.verbose)
        if ok:
            log.success(f"{name}: fetched")
        else:
            log.error(f"{name}: fetch failed")
            failed.append(name)
    if failed:
        log.error(f"Fetch failed for: {', '.join(failed)}")
        return 1
    return 0


def cmd_git_pull(args: argparse.Namespace) -> int:
    """Run ``git pull`` on every repo."""
    log.banner("Git Pull", "Pulling latest commits for all repos")
    repos = _repos()
    failed = []
    for repo in repos:
        from pathlib import Path
        path = Path(repo["dir"])
        name = repo["name"]
        if not gitutil.is_git_repo(path):
            log.warn(f"{name}: not a git repo – skipping")
            continue
        log.info(f"{name}: pulling…")
        ok = gitutil.pull(path, verbose=args.verbose)
        if ok:
            log.success(f"{name}: up to date")
        else:
            log.error(f"{name}: pull failed")
            failed.append(name)
    if failed:
        log.error(f"Pull failed for: {', '.join(failed)}")
        return 1
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# repo (Google repo tool) sub-commands
# ─────────────────────────────────────────────────────────────────────────────

def _require_repo() -> bool:
    """Log an error and return False if the repo tool is unavailable."""
    if not repotool.is_available():
        log.error(
            "Google repo tool not found on PATH.\n"
            "Install:  https://android.googlesource.com/tools/repo"
        )
        return False
    return True


def cmd_repo_manifest(args: argparse.Namespace) -> int:
    """Show the manifest projects table, or modify the manifest."""
    if not _require_repo():
        return 1

    # ── manifest sub-sub-command ──────────────────────────────────────────
    sub = getattr(args, "manifest_command", None)

    if sub == "show" or sub is None:
        log.banner("Repo Manifest", f"{cfg.WORKSPACE / '.repo' / 'manifests' / 'default.xml'}")
        repotool.print_manifest_table(cfg.WORKSPACE)
        return 0

    try:
        m = repotool.Manifest(cfg.WORKSPACE)
    except FileNotFoundError as exc:
        log.error(str(exc))
        return 1

    if sub == "set-revision":
        if args.project:
            ok = m.set_project_revision(args.project, args.revision)
            if not ok:
                log.error(f"Project '{args.project}' not found in manifest.")
                return 1
            log.success(f"Set revision of '{args.project}' → '{args.revision}'")
        else:
            m.set_default_revision(args.revision)
            log.success(f"Set default revision → '{args.revision}'")
        m.save()
        repotool.print_manifest_table(cfg.WORKSPACE)
        return 0

    if sub == "clear-revision":
        ok = m.clear_project_revision(args.project)
        if not ok:
            log.error(f"Project '{args.project}' not found in manifest.")
            return 1
        log.success(f"Cleared revision override for '{args.project}' (now inherits default).")
        m.save()
        return 0

    if sub == "add":
        ok = m.add_project(
            args.name,
            args.path,
            revision=args.revision or None,
            remote=args.remote or None,
            groups=args.groups or None,
        )
        if not ok:
            log.error(f"Project '{args.name}' / path '{args.path}' already exists.")
            return 1
        log.success(f"Added project '{args.name}' at path '{args.path}'.")
        m.save()
        repotool.print_manifest_table(cfg.WORKSPACE)
        return 0

    if sub == "remove":
        ok = m.remove_project(args.project)
        if not ok:
            log.error(f"Project '{args.project}' not found in manifest.")
            return 1
        log.success(f"Removed project '{args.project}' from manifest.")
        m.save()
        return 0

    return 0


def cmd_repo_status(args: argparse.Namespace) -> int:
    """Show ``repo status`` output for all projects."""
    if not _require_repo():
        return 1
    log.banner("Repo Status")
    repotool.print_repo_status(cfg.WORKSPACE)
    return 0


def cmd_repo_info(args: argparse.Namespace) -> int:
    """Show ``repo info`` (manifest branch, remotes, current revisions)."""
    if not _require_repo():
        return 1
    log.banner("Repo Info")
    repotool.print_repo_info(cfg.WORKSPACE)
    return 0


def cmd_repo_sync(args: argparse.Namespace) -> int:
    """Run ``repo sync`` to fetch and update all projects."""
    if not _require_repo():
        return 1
    log.banner(
        "Repo Sync",
        f"jobs: {args.jobs}  |  projects: {', '.join(args.projects) if args.projects else 'all'}",
    )
    ok = repotool.sync(
        cfg.WORKSPACE,
        projects=args.projects or None,
        jobs=args.jobs,
        verbose=args.verbose,
    )
    if ok:
        log.success("repo sync completed.")
    else:
        log.error("repo sync failed.")
    return 0 if ok else 1


def cmd_repo_forall(args: argparse.Namespace) -> int:
    """Run an arbitrary shell command in every project via ``repo forall``."""
    if not _require_repo():
        return 1
    log.banner("Repo Forall", f"$ {args.cmd}")
    ok = repotool.forall(cfg.WORKSPACE, args.cmd, verbose=args.verbose)
    return 0 if ok else 1


def cmd_repo_checkout(args: argparse.Namespace) -> int:
    """Switch every project to *branch* (optionally creating it)."""
    if not _require_repo():
        return 1
    log.banner(
        "Repo Checkout",
        f"branch: {args.branch}  |  create: {args.create}  |  force: {args.force}",
    )
    ok = repotool.checkout_branch(
        cfg.WORKSPACE,
        args.branch,
        create=args.create,
        force=args.force,
    )
    return 0 if ok else 1


# ── project sub-commands ─────────────────────────────────────────────────────

def _find_project_by_name(name: str):  # -> tuple[dict | None, hooksmod.ProjectManifest | None]
    """
    Look up a project by name (case-insensitive) from the scanned workspace.
    Returns ``(project_dict, manifest)`` or ``(None, None)`` on failure.
    The manifest may be None if no ``project.json`` exists.
    """
    projects = cfg.get_projects()
    matched = [p for p in projects if p["name"].lower() == name.lower()]
    if not matched:
        names = ", ".join(p["name"] for p in projects)
        log.error(f"Project '{name}' not found. Available: {names}")
        return None, None
    project = matched[0]
    manifest = hooksmod.ProjectManifest.load(Path(project["dir"]))
    if manifest is None:
        log.error(
            f"No project.json found in {project['dir']}.\n"
            f"Create one with:  project init {Path(project['dir'])}"
        )
        return project, None
    return project, manifest


def cmd_project_list(args: argparse.Namespace) -> int:
    """List all workspace projects discovered from project.json files."""
    log.banner("Workspace Projects")
    projects = cfg.get_projects()
    if not projects:
        log.warn("No projects found. Add a project.json + pom.xml to a sub-directory.")
        return 0

    try:
        from rich.table import Table
        from rich.console import Console

        table = Table(title=f"Projects  ({len(projects)}  in build order)", show_lines=True)
        table.add_column("#",          justify="right", style="dim")
        table.add_column("Name",       style="bold cyan",  no_wrap=True)
        table.add_column("Type",       style="bold")
        table.add_column("G:A:V",      style="dim")
        table.add_column("Deps",       style="dim")
        table.add_column("Built",      justify="center")
        table.add_column("Dir",        style="dim", overflow="fold")

        for i, p in enumerate(projects, 1):
            m = hooksmod.ProjectManifest.load(Path(p["dir"]))
            art = Path(p["artifact"]) if p.get("artifact") else None
            built = "✔" if (art and art.exists()) else "✖"
            if m:
                gav  = f"{m.group_id}:{m.artifact_id}:{m.version}"
                deps = ", ".join(d["artifactId"] for d in m.workspace_deps) or "—"
                type_str = (
                    f"[yellow]{m.project_type}[/yellow]"
                    if m.project_type == "application"
                    else f"[blue]{m.project_type}[/blue]"
                )
            else:
                gav = deps = type_str = "[dim]no project.json[/dim]"
            table.add_row(str(i), p["name"], type_str, gav, deps, built,
                          str(Path(p["dir"]).relative_to(cfg.WORKSPACE)))
        Console().print(table)

    except ImportError:
        print(f"\n{'#':<3}  {'Name':<16}  {'G:A:V':<40}  Built")
        print("─" * 80)
        for i, p in enumerate(projects, 1):
            m = hooksmod.ProjectManifest.load(Path(p["dir"]))
            art = Path(p["artifact"]) if p.get("artifact") else None
            built = "✔" if (art and art.exists()) else "✖"
            gav = f"{m.group_id}:{m.artifact_id}:{m.version}" if m else "—"
            print(f"{i:<3}  {p['name']:<16}  {gav:<40}  {built}")
        print()
    return 0


def cmd_project_show(args: argparse.Namespace) -> int:
    """Pretty-print project.json for a named project."""
    project, manifest = _find_project_by_name(args.project)
    if manifest is None:
        return 1
    log.banner(f"Project Manifest – {manifest.name}")
    print(manifest.path.read_text(encoding="utf-8"))
    return 0


def cmd_project_init(args: argparse.Namespace) -> int:
    """
    Create a starter project.json in a directory.

    The directory can be:
      - A name of an existing workspace project (must have a pom.xml).
      - An absolute or relative path to a directory.

    If the directory already has a project.json, use --force to overwrite.
    """
    target_dir = None  # type: Path | None

    # Try to resolve as a project name first
    projects = cfg.get_projects()
    by_name = {p["name"].lower(): Path(p["dir"]) for p in projects}
    if args.dir.lower() in by_name:
        target_dir = by_name[args.dir.lower()]
    else:
        candidate = Path(args.dir).expanduser().resolve()
        if candidate.is_dir():
            target_dir = candidate
        else:
            # Maybe it's a name of a new project to create inside the workspace
            target_dir = cfg.WORKSPACE / args.dir
            target_dir.mkdir(parents=True, exist_ok=True)
            log.info(f"Created directory: {target_dir}")

    dest = target_dir / "project.json"
    if dest.exists() and not args.force:
        log.warn(f"{dest} already exists. Pass --force to overwrite.")
        return 1

    # Derive identity from pom.xml if available
    pom = target_dir / "pom.xml"
    group_id = artifact_id = version = ""
    if pom.exists():
        try:
            import xml.etree.ElementTree as _ET
            root = _ET.parse(str(pom)).getroot()
            ns   = "http://maven.apache.org/POM/4.0.0"
            group_id    = (root.findtext(f"{{{ns}}}groupId")    or "").strip()
            artifact_id = (root.findtext(f"{{{ns}}}artifactId") or "").strip()
            version     = (root.findtext(f"{{{ns}}}version")    or "").strip()
        except Exception:
            pass

    name = args.name or artifact_id or target_dir.name
    data = {
        "name":        name,
        "groupId":     args.group_id or group_id or "works.nuka",
        "artifactId":  args.artifact_id or artifact_id or name,
        "version":     args.version or version or "1.0.0",
        "type":        args.type,
        "description": args.description or "",
        "build": {
            "strip_gpg_unless_release": args.type == "library",
            "nightly_suffix_on_devel":  args.type == "library",
        },
        "workspace_dependencies": [],
    }
    dest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    log.success(f"Created {dest}")
    print(dest.read_text(encoding="utf-8"))

    # Invalidate the project cache so subsequent commands see the new project
    cfg._projects_cache = None
    return 0


def _sync_poms_after_manifest_change(changed_manifest: "hooksmod.ProjectManifest") -> None:
    """
    After a project.json is saved, patch pom.xml files to keep versions in sync:

    1. The changed project's own pom.xml  (its identity block: groupId/artifactId/version).
    2. Every other workspace project whose workspace_dependencies include the
       changed project  (so their <dependency> version is updated too).
    """
    cfg._projects_cache = None   # force re-scan with fresh manifests

    # Build a full {artifactId: manifest} map from the current workspace
    workspace = cfg.WORKSPACE
    all_manifests: dict[str, hooksmod.ProjectManifest] = {}
    for entry in sorted(workspace.iterdir()):
        if not entry.is_dir():
            continue
        try:
            m = hooksmod.ProjectManifest.load(entry)
        except ValueError:
            m = None
        if m is not None:
            all_manifests[m.artifact_id] = m

    patched = []

    # 1. Patch the changed project's own pom
    if hooksmod.sync_pom_versions(changed_manifest.path.parent, all_manifests):
        patched.append(changed_manifest.name)

    # Sync module.json if the project has a module block
    hooksmod.sync_module_json(changed_manifest)

    # 2. Patch every dependent project
    for aid, m in all_manifests.items():
        if m.artifact_id == changed_manifest.artifact_id:
            continue
        dep_ids = {d.get("artifactId") for d in m.workspace_deps}
        if changed_manifest.artifact_id in dep_ids:
            if hooksmod.sync_pom_versions(m.path.parent, all_manifests):
                patched.append(m.name)

    if patched:
        log.success(f"pom.xml synced: {', '.join(patched)}")


def cmd_project_set(args: argparse.Namespace) -> int:
    """Set a field on a project's project.json."""
    project, manifest = _find_project_by_name(args.project)
    if manifest is None:
        return 1

    field_name = args.field
    value      = args.value

    if field_name == "version":
        manifest.version = value
    elif field_name == "type":
        if value not in hooksmod.PROJECT_TYPES:
            log.error(f"Invalid type '{value}'. Must be: {hooksmod.PROJECT_TYPES}")
            return 1
        manifest.project_type = value
    elif field_name == "groupId":
        manifest.group_id = value
    elif field_name == "artifactId":
        manifest.artifact_id = value
    elif field_name == "description":
        manifest.description = value
    elif field_name == "strip_gpg":
        manifest.build["strip_gpg_unless_release"] = value.lower() in ("true", "1", "yes")
    elif field_name == "nightly":
        manifest.build["nightly_suffix_on_devel"] = value.lower() in ("true", "1", "yes")
    else:
        log.error(
            f"Unknown field '{field_name}'. Choose from: "
            "version, type, groupId, artifactId, description, strip_gpg, nightly"
        )
        return 1

    manifest.save()
    _sync_poms_after_manifest_change(manifest)
    log.success(f"Updated {manifest.path.name}:  {field_name} = {value}")
    print(manifest.path.read_text(encoding="utf-8"))
    return 0


def cmd_project_add_dep(args: argparse.Namespace) -> int:
    """Add a workspace dependency to a project's project.json."""
    project, manifest = _find_project_by_name(args.project)
    if manifest is None:
        return 1

    new_dep = {"groupId": args.group_id, "artifactId": args.artifact_id}
    for dep in manifest.workspace_deps:
        if dep["groupId"] == new_dep["groupId"] and dep["artifactId"] == new_dep["artifactId"]:
            log.warn(f"Dependency {args.group_id}:{args.artifact_id} already declared.")
            return 0

    manifest.workspace_deps.append(new_dep)
    manifest.save()
    _sync_poms_after_manifest_change(manifest)
    log.success(f"Added workspace dep {args.group_id}:{args.artifact_id} to {manifest.name}")
    print(manifest.path.read_text(encoding="utf-8"))
    return 0


def cmd_project_remove_dep(args: argparse.Namespace) -> int:
    """Remove a workspace dependency from a project's project.json."""
    project, manifest = _find_project_by_name(args.project)
    if manifest is None:
        return 1

    before = len(manifest.workspace_deps)
    manifest.workspace_deps = [
        d for d in manifest.workspace_deps
        if not (
            d["artifactId"] == args.artifact_id
            and (args.group_id is None or d["groupId"] == args.group_id)
        )
    ]
    if len(manifest.workspace_deps) == before:
        log.warn(f"Dependency '{args.artifact_id}' not found in {manifest.name}.")
        return 1
    manifest.save()
    _sync_poms_after_manifest_change(manifest)
    log.success(f"Removed workspace dep '{args.artifact_id}' from {manifest.name}")
    print(manifest.path.read_text(encoding="utf-8"))
    return 0


def cmd_project_run(args: argparse.Namespace) -> int:
    """Dry-run pre-build hooks for a specific project (no Maven build)."""
    mode   = args.mode or cfg.BUILD_MODE
    target = args.project

    projects = cfg.get_projects()
    matched = [p for p in projects if p["name"].lower() == target.lower()]
    if not matched:
        names = ", ".join(p["name"] for p in projects)
        log.error(f"Project '{target}' not found. Available: {names}")
        return 1

    project = matched[0]
    log.banner(
        f"Project hooks – {project['name']}",
        f"mode: {mode}  |  phase: {args.phase}",
    )

    ctx = hooksmod.build_hook_context(project, mode=mode, verbose=args.verbose,
                                      workspace_dir=cfg.WORKSPACE)
    hook_table = _universal_hooks()
    fns = hook_table.get(args.phase, [])

    if not fns:
        log.warn(f"No {args.phase} hooks for '{project['name']}'.")
        return 0

    ok, pom_override, extra_mvn_args = hooksmod.run_hooks(args.phase, fns, ctx)
    if ok:
        if pom_override:
            log.success(f"pom override → {pom_override}")
        if extra_mvn_args:
            log.info(f"extra Maven args → {extra_mvn_args}")
        log.success("All hooks passed.")
    else:
        log.error("One or more hooks failed.")
    return 0 if ok else 1


# ── sdk sub-commands ──────────────────────────────────────────────────────────

def cmd_sdk_list(args: argparse.Namespace) -> int:
    """List locally installed Java candidates known to sdkman."""
    log.banner("sdkman – Installed Java Candidates")
    if not sdkman.is_available():
        log.error("sdkman is not installed (expected at ~/.sdkman).")
        return 1
    sdkman.print_candidates()
    return 0


def cmd_sdk_install(args: argparse.Namespace) -> int:
    """Install a Java candidate via sdkman."""
    if not sdkman.is_available():
        log.error("sdkman is not installed (expected at ~/.sdkman).")
        return 1
    ok = sdkman.install(args.identifier)
    return 0 if ok else 1


def cmd_sdk_use(args: argparse.Namespace) -> int:
    """
    Switch the default sdkman Java candidate AND update JAVA_VERSION in config
    for this session. Prints the export command to make it permanent.
    """
    if not sdkman.is_available():
        log.error("sdkman is not installed (expected at ~/.sdkman).")
        return 1

    home = sdkman.resolve_java_home(args.identifier)
    if home is None:
        log.warn(f"{args.identifier} is not installed locally.")
        if args.install:
            log.info(f"Installing {args.identifier}…")
            if not sdkman.install(args.identifier):
                return 1
            home = sdkman.resolve_java_home(args.identifier)
        else:
            log.info("Pass --install to install it automatically.")
            return 1

    log.success(f"JAVA_HOME resolved: {home}")
    log.info(
        f"To make this permanent, set the environment variable:\n"
        f"  export ISLANDS_JAVA_VERSION={args.identifier}"
    )
    # Patch the in-process cfg so subsequent commands in the same run use it
    cfg.JAVA_VERSION = args.identifier
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# idea command – generate IntelliJ IDEA monorepo project files
# ─────────────────────────────────────────────────────────────────────────────

def _pretty_xml(element: ET.Element) -> str:
    """Return a pretty-printed XML string for the element."""
    raw = ET.tostring(element, encoding="unicode")
    dom = minidom.parseString(raw)
    lines = dom.toprettyxml(indent="  ").splitlines()
    # minidom adds an XML declaration on line 0; keep it
    return "\n".join(lines) + "\n"


def cmd_idea(args: argparse.Namespace) -> int:
    """Generate / refresh IntelliJ IDEA .idea monorepo project files."""
    log.banner("IDEA Project Setup", "Generating IntelliJ IDEA monorepo configuration")

    idea_dir = cfg.WORKSPACE / ".idea"
    idea_dir.mkdir(exist_ok=True)

    java_ver = args.java_version or cfg.JAVA_VERSION or "24"
    java_major = java_ver.split(".")[0].split("-")[0]
    lang_level = f"JDK_{java_major}"

    # Discover Maven modules dynamically
    maven_modules = [
        (p["name"], Path(p["dir"]))
        for p in cfg.get_projects()
    ]

    # ── modules.xml ──────────────────────────────────────────────────────────
    project_el = ET.Element("project", version="4")
    mgr = ET.SubElement(project_el, "component", name="ProjectModuleManager")
    modules_el = ET.SubElement(mgr, "modules")

    # Build module (Python)
    build_iml = "$PROJECT_DIR$/Build/Build.iml"
    ET.SubElement(modules_el, "module",
                  fileurl=f"file://{build_iml}",
                  filepath=build_iml)

    # Root IDEA module
    root_iml = f"$PROJECT_DIR$/.idea/{cfg.WORKSPACE.name}.iml"
    ET.SubElement(modules_el, "module",
                  fileurl=f"file://{root_iml}",
                  filepath=root_iml)

    # Maven sub-projects
    for name, project_dir in maven_modules:
        rel = project_dir.relative_to(cfg.WORKSPACE)
        iml_rel = f"$PROJECT_DIR$/{rel}/{name}.iml"
        ET.SubElement(modules_el, "module",
                      fileurl=f"file://{iml_rel}",
                      filepath=iml_rel,
                      group="Maven Projects")

    modules_xml_path = idea_dir / "modules.xml"
    modules_xml_path.write_text(_pretty_xml(project_el), encoding="utf-8")
    log.success(f"Written: {modules_xml_path.relative_to(cfg.WORKSPACE)}")

    # ── misc.xml ─────────────────────────────────────────────────────────────
    misc_el = ET.Element("project", version="4")
    black = ET.SubElement(misc_el, "component", name="Black")
    opt = ET.SubElement(black, "option", name="sdkName")
    opt.set("value", "Python 3.13")
    root_mgr = ET.SubElement(misc_el, "component",
                              name="ProjectRootManager",
                              version="2",
                              languageLevel=lang_level,
                              **{"project-jdk-name": f"Java {java_major}",
                                 "project-jdk-type": "JavaSDK"})
    ET.SubElement(root_mgr, "output", url="file://$PROJECT_DIR$/out")
    misc_xml_path = idea_dir / "misc.xml"
    misc_xml_path.write_text(_pretty_xml(misc_el), encoding="utf-8")
    log.success(f"Written: {misc_xml_path.relative_to(cfg.WORKSPACE)}")

    # ── .iml files for each Maven sub-project ────────────────────────────────
    for name, project_dir in maven_modules:
        iml_path = project_dir / f"{name}.iml"
        if iml_path.exists() and not args.force:
            log.info(f"Skipping (already exists): {iml_path.relative_to(cfg.WORKSPACE)}")
            continue
        iml_el = ET.Element("module", type="JAVA_MODULE", version="4")
        root_mgr_el = ET.SubElement(iml_el, "component",
                                     name="NewModuleRootManager",
                                     **{"inherit-compiler-output": "true"})
        ET.SubElement(root_mgr_el, "exclude-output")
        content = ET.SubElement(root_mgr_el, "content", url="file://$MODULE_DIR$")
        if (project_dir / "pom.xml").exists():
            ET.SubElement(content, "excludeFolder", url="file://$MODULE_DIR$/target")
        ET.SubElement(root_mgr_el, "orderEntry", type="inheritedJdk")
        ET.SubElement(root_mgr_el, "orderEntry", type="sourceFolder", forTests="false")
        iml_path.write_text(_pretty_xml(iml_el), encoding="utf-8")
        log.success(f"Written: {iml_path.relative_to(cfg.WORKSPACE)}")

    # ── vcs.xml ──────────────────────────────────────────────────────────────
    vcs_path = idea_dir / "vcs.xml"
    if not vcs_path.exists() or args.force:
        vcs_el = ET.Element("project", version="4")
        vcs_comp = ET.SubElement(vcs_el, "component", name="VcsDirectoryMappings")
        ET.SubElement(vcs_comp, "mapping", directory="$PROJECT_DIR$", vcs="Git")
        vcs_path.write_text(_pretty_xml(vcs_el), encoding="utf-8")
        log.success(f"Written: {vcs_path.relative_to(cfg.WORKSPACE)}")

    # ── encodings.xml ─────────────────────────────────────────────────────────
    enc_path = idea_dir / "encodings.xml"
    if not enc_path.exists() or args.force:
        enc_el = ET.Element("project", version="4")
        ET.SubElement(enc_el, "component",
                      name="Encoding",
                      addBOMForNewFiles=";UTF-8:with NO BOM",
                      defaultCharsetForPropertiesFiles="UTF-8")
        enc_path.write_text(_pretty_xml(enc_el), encoding="utf-8")
        log.success(f"Written: {enc_path.relative_to(cfg.WORKSPACE)}")

    # ── compiler.xml ──────────────────────────────────────────────────────────
    compiler_path = idea_dir / "compiler.xml"
    comp_el = ET.Element("project", version="4")
    compiler_comp = ET.SubElement(comp_el, "component", name="CompilerConfiguration")
    ET.SubElement(compiler_comp, "bytecodeTargetLevel", **{"target": java_major})
    compiler_path.write_text(_pretty_xml(comp_el), encoding="utf-8")
    log.success(f"Written: {compiler_path.relative_to(cfg.WORKSPACE)}")

    log.banner(
        "Done",
        textwrap.dedent(f"""\
        Open the workspace root in IntelliJ IDEA:
          File → Open → {cfg.WORKSPACE}
        Then: File → Project Structure → Modules to verify all {len(maven_modules)} modules.
        If prompted, import Maven projects from the pom.xml files in each module.""")
    )
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# CLI parser
# ─────────────────────────────────────────────────────────────────────────────

def _add_java_version_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--java-version", metavar="ID", default=None,
        help=(
            f"sdkman Java candidate to use, e.g. '24.0.2-tem' "
            f"(default: {cfg.JAVA_VERSION or 'ambient PATH'}). "
            "Overrides ISLANDS_JAVA_VERSION env var."
        ),
    )


def _add_mode_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--mode", metavar="MODE", default=None,
        choices=["local", "devel", "release"],
        help=(
            "Build mode passed to pre-build hooks "
            "(local | devel | release). "
            f"Default: {cfg.BUILD_MODE} (from ISLANDS_BUILD_MODE env var). "
            "'local' strips the GPG plugin; 'devel' also appends "
            "-nightly_<sha> to the version; 'release' leaves the pom untouched."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build",
        description="Islands Build Automation CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version="islands-build 1.0.0")

    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    # ── build-all ─────────────────────────────────────────────────────────────
    p_build = sub.add_parser(
        "build-all",
        help="Build all projects (ModularKit → CoffeeLoader → Islands)",
        description="Build every project in dependency order using 'mvn clean install'.",
    )
    p_build.add_argument("--with-tests", action="store_true",
        help="Run unit tests during build (default: tests are skipped)")
    p_build.add_argument("--clean", action="store_true",
        help="Run 'mvn clean install' instead of 'mvn install' (default: no clean)")
    p_build.add_argument("--verbose", "-v", action="store_true",
        help="Show full Maven output (removes --batch-mode)")
    _add_java_version_arg(p_build)
    _add_mode_arg(p_build)
    p_build.set_defaults(func=cmd_build_all)

    # ── run-islands ───────────────────────────────────────────────────────────
    p_run = sub.add_parser(
        "run-islands",
        help="Build all, assemble output dir, then launch Islands via CoffeeLoader",
        description=(
            "Full pipeline:\n"
            "  1. mvn clean install  ModularKit\n"
            "  2. mvn clean install  CoffeeLoader\n"
            "  3. mvn clean install  Islands\n"
            "  4. Assemble output/ directory\n"
            "  5. Write CoffeeLoader config.json  (sources -> output/modules/)\n"
            "  6. java -jar CoffeeLoader          (blocks until Ctrl+C)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_run.add_argument("--with-tests", action="store_true",
        help="Run unit tests during build")
    p_run.add_argument("--fast-build", action="store_true", dest="fast_build",
        help="Skip the Maven build entirely and go straight to assemble + launch "
             "(uses whatever artifacts are already on disk)")
    p_run.add_argument("--clean", action="store_true",
        help="Run 'mvn clean install' and wipe the output directory before assembling "
             "(default: 'mvn install' only, output directory is kept)")
    p_run.add_argument("--verbose", "-v", action="store_true",
        help="Show full Maven output")
    p_run.add_argument("--java-opts", metavar="OPTS", default=None,
        help="Extra JVM options before -jar, e.g. --java-opts '-Xmx512m'")
    _add_java_version_arg(p_run)
    _add_mode_arg(p_run)
    p_run.set_defaults(func=cmd_run_islands)

    # ── assemble ──────────────────────────────────────────────────────────────
    p_asm = sub.add_parser(
        "assemble",
        help="Copy built artifacts into output/ without rebuilding",
    )
    p_asm.add_argument("--clean", action="store_true",
        help="Wipe the output directory before assembling (default: keep existing contents)")
    p_asm.set_defaults(func=cmd_assemble)

    # ── clean ─────────────────────────────────────────────────────────────────
    p_clean = sub.add_parser("clean", help="Delete the output/ directory")
    p_clean.set_defaults(func=cmd_clean)

    # ── status ────────────────────────────────────────────────────────────────
    p_status = sub.add_parser("status", help="Show build status of each Maven artifact")
    p_status.set_defaults(func=cmd_status)

    # ── info ──────────────────────────────────────────────────────────────────
    p_info = sub.add_parser("info", help="Print resolved workspace paths and Java config")
    p_info.set_defaults(func=cmd_info)

    # ── idea ──────────────────────────────────────────────────────────────────
    p_idea = sub.add_parser(
        "idea",
        help="Generate / refresh IntelliJ IDEA .idea monorepo project files",
        description=(
            "Creates / updates .idea/modules.xml, .idea/misc.xml, .idea/compiler.xml,\n"
            ".idea/vcs.xml, .idea/encodings.xml and a MODULE.iml for each Maven module,\n"
            "so the workspace root can be opened as a single IDEA project containing\n"
            "all discovered Maven projects as module entries."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_idea.add_argument(
        "--force", "-f", action="store_true",
        help="Overwrite existing .iml and helper files (default: skip if present)",
    )
    _add_java_version_arg(p_idea)
    p_idea.set_defaults(func=cmd_idea)

    # ── git ───────────────────────────────────────────────────────────────────
    p_git = sub.add_parser(
        "git",
        help="Git repository management across all repos",
        description="Manage all git repos.\n\nSub-commands: status | branches | checkout | fetch | pull",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    git_sub = p_git.add_subparsers(dest="git_command", metavar="<git-command>")
    git_sub.required = True

    # git status
    p_git_status = git_sub.add_parser("status", help="Show branch and working-tree status for all repos")
    p_git_status.set_defaults(func=cmd_git_status)

    # git branches
    p_git_branches = git_sub.add_parser("branches", help="List all local branches for all repos")
    p_git_branches.set_defaults(func=cmd_git_branches)

    # git checkout
    p_git_checkout = git_sub.add_parser("checkout", help="Switch all repos to a given branch")
    p_git_checkout.add_argument("branch", metavar="BRANCH")
    p_git_checkout.add_argument("--create", "-b", action="store_true")
    p_git_checkout.add_argument("--force",  "-f", action="store_true")
    p_git_checkout.set_defaults(func=cmd_git_checkout)

    # git fetch
    p_git_fetch = git_sub.add_parser("fetch", help="Run 'git fetch --all --prune' on every repo")
    p_git_fetch.add_argument("--verbose", "-v", action="store_true")
    p_git_fetch.set_defaults(func=cmd_git_fetch)

    # git pull
    p_git_pull = git_sub.add_parser("pull", help="Run 'git pull' on every repo")
    p_git_pull.add_argument("--verbose", "-v", action="store_true")
    p_git_pull.set_defaults(func=cmd_git_pull)

    # ── repo (Google repo tool) ────────────────────────────────────────────────
    p_repo = sub.add_parser("repo", help="Google repo tool management")
    repo_sub = p_repo.add_subparsers(dest="repo_command", metavar="<repo-command>")
    repo_sub.required = True

    p_rm = repo_sub.add_parser("manifest", help="Show or modify the default.xml manifest")
    rm_sub = p_rm.add_subparsers(dest="manifest_command", metavar="<manifest-command>")
    p_rm_show = rm_sub.add_parser("show")
    p_rm_show.set_defaults(func=cmd_repo_manifest)
    p_rm_setrev = rm_sub.add_parser("set-revision")
    p_rm_setrev.add_argument("revision", metavar="REVISION")
    p_rm_setrev.add_argument("--project", "-p", metavar="NAME_OR_PATH", default=None)
    p_rm_setrev.set_defaults(func=cmd_repo_manifest)
    p_rm_clrrev = rm_sub.add_parser("clear-revision")
    p_rm_clrrev.add_argument("--project", "-p", metavar="NAME_OR_PATH", required=True)
    p_rm_clrrev.set_defaults(func=cmd_repo_manifest)
    p_rm_add = rm_sub.add_parser("add")
    p_rm_add.add_argument("name", metavar="NAME")
    p_rm_add.add_argument("path", metavar="PATH")
    p_rm_add.add_argument("--revision", metavar="REV",    default=None)
    p_rm_add.add_argument("--remote",   metavar="REMOTE", default=None)
    p_rm_add.add_argument("--groups",   metavar="GROUPS", default=None)
    p_rm_add.set_defaults(func=cmd_repo_manifest)
    p_rm_del = rm_sub.add_parser("remove")
    p_rm_del.add_argument("project", metavar="NAME_OR_PATH")
    p_rm_del.set_defaults(func=cmd_repo_manifest)
    p_rm.set_defaults(func=cmd_repo_manifest)

    p_repo_status = repo_sub.add_parser("status")
    p_repo_status.set_defaults(func=cmd_repo_status)
    p_repo_info = repo_sub.add_parser("info")
    p_repo_info.set_defaults(func=cmd_repo_info)
    p_repo_sync = repo_sub.add_parser("sync")
    p_repo_sync.add_argument("projects", metavar="PROJECT", nargs="*")
    p_repo_sync.add_argument("-j", "--jobs", type=int, default=4, metavar="N")
    p_repo_sync.add_argument("--verbose", "-v", action="store_true")
    p_repo_sync.set_defaults(func=cmd_repo_sync)
    p_repo_forall = repo_sub.add_parser("forall")
    p_repo_forall.add_argument("cmd", metavar="COMMAND")
    p_repo_forall.add_argument("--verbose", "-v", action="store_true")
    p_repo_forall.set_defaults(func=cmd_repo_forall)
    p_repo_co = repo_sub.add_parser("checkout")
    p_repo_co.add_argument("branch", metavar="BRANCH")
    p_repo_co.add_argument("--create", "-b", action="store_true")
    p_repo_co.add_argument("--force",  "-f", action="store_true")
    p_repo_co.set_defaults(func=cmd_repo_checkout)

    # ── project ───────────────────────────────────────────────────────────────
    p_proj = sub.add_parser(
        "project",
        help="Manage workspace projects and their project.json manifests",
        description=(
            "Manage workspace projects discovered via project.json files.\n\n"
            "Sub-commands:\n"
            "  list                       list all discovered projects\n"
            "  show   <PROJECT>           print project.json\n"
            "  init   <DIR> [options]     create a new project.json\n"
            "  set    <PROJECT> <F> <V>   edit a manifest field\n"
            "  add-dep    <P> <G> <A>     add a workspace dependency\n"
            "  remove-dep <P> <A>         remove a workspace dependency\n"
            "  run    <PROJECT>           dry-run pre_build hooks\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    proj_sub = p_proj.add_subparsers(dest="project_command", metavar="<project-command>")
    proj_sub.required = True

    p_proj_list = proj_sub.add_parser("list", help="List all workspace projects")
    p_proj_list.set_defaults(func=cmd_project_list)

    p_proj_show = proj_sub.add_parser("show", help="Print project.json for a project")
    p_proj_show.add_argument("project", metavar="PROJECT")
    p_proj_show.set_defaults(func=cmd_project_show)

    p_proj_init = proj_sub.add_parser(
        "init",
        help="Create a starter project.json (reads pom.xml if present)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_proj_init.add_argument("dir", metavar="DIR",
        help="Project name, absolute path, or new folder name to initialise")
    p_proj_init.add_argument("--type", default="library", choices=["library", "application"])
    p_proj_init.add_argument("--name",        default=None,
        help="Override project name (default: derived from pom.xml or dir name)")
    p_proj_init.add_argument("--group-id",    dest="group_id",    default=None,
        help="Override groupId (default: from pom.xml or 'works.nuka')")
    p_proj_init.add_argument("--artifact-id", dest="artifact_id", default=None,
        help="Override artifactId (default: from pom.xml or dir name)")
    p_proj_init.add_argument("--version",     default=None,
        help="Override version (default: from pom.xml or '1.0.0')")
    p_proj_init.add_argument("--description", default=None, help="Project description")
    p_proj_init.add_argument("--force", "-f", action="store_true",
        help="Overwrite existing project.json")
    p_proj_init.set_defaults(func=cmd_project_init)

    p_proj_set = proj_sub.add_parser("set",
        help="Set a field in project.json")
    p_proj_set.add_argument("project", metavar="PROJECT")
    p_proj_set.add_argument("field",   metavar="FIELD",
        choices=["version","type","groupId","artifactId","description","strip_gpg","nightly"])
    p_proj_set.add_argument("value",   metavar="VALUE")
    p_proj_set.set_defaults(func=cmd_project_set)

    p_proj_adddep = proj_sub.add_parser("add-dep",
        help="Add a workspace dependency to project.json")
    p_proj_adddep.add_argument("project",     metavar="PROJECT")
    p_proj_adddep.add_argument("group_id",    metavar="GROUP_ID")
    p_proj_adddep.add_argument("artifact_id", metavar="ARTIFACT_ID")
    p_proj_adddep.set_defaults(func=cmd_project_add_dep)

    p_proj_rmdep = proj_sub.add_parser("remove-dep",
        help="Remove a workspace dependency from project.json")
    p_proj_rmdep.add_argument("project",     metavar="PROJECT")
    p_proj_rmdep.add_argument("artifact_id", metavar="ARTIFACT_ID")
    p_proj_rmdep.add_argument("--group-id",  metavar="GROUP_ID", default=None, dest="group_id")
    p_proj_rmdep.set_defaults(func=cmd_project_remove_dep)

    p_proj_run = proj_sub.add_parser("run",
        help="Dry-run pre_build hooks for a project (no Maven build)")
    p_proj_run.add_argument("project", metavar="PROJECT")
    p_proj_run.add_argument("--phase", metavar="PHASE", default="pre_build",
        choices=["pre_build", "post_build"])
    p_proj_run.add_argument("--verbose", "-v", action="store_true")
    _add_mode_arg(p_proj_run)
    p_proj_run.set_defaults(func=cmd_project_run)

    # ── sdk ───────────────────────────────────────────────────────────────────
    p_sdk = sub.add_parser("sdk", help="Manage Java installations via sdkman")
    sdk_sub = p_sdk.add_subparsers(dest="sdk_command", metavar="<sdk-command>")
    sdk_sub.required = True

    p_sdk_list = sdk_sub.add_parser("list", help="List locally installed Java candidates")
    p_sdk_list.set_defaults(func=cmd_sdk_list)

    p_sdk_inst = sdk_sub.add_parser("install", help="Install a Java candidate (e.g. 24.0.2-tem)")
    p_sdk_inst.add_argument("identifier", metavar="IDENTIFIER")
    p_sdk_inst.set_defaults(func=cmd_sdk_install)

    p_sdk_use = sdk_sub.add_parser("use", help="Switch the active Java candidate")
    p_sdk_use.add_argument("identifier", metavar="IDENTIFIER")
    p_sdk_use.add_argument("--install", action="store_true",
        help="Install the candidate first if not already available")
    p_sdk_use.set_defaults(func=cmd_sdk_use)

    return parser


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))

if __name__ == "__main__":
    main()
