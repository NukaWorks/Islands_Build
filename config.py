"""
Central configuration for the Islands build automation system.
All paths are resolved relative to the workspace root.
Projects are discovered dynamically by scanning for project.json files –
no hardcoded project names or artifact paths.
"""
import os
from pathlib import Path

# ── Build mode ────────────────────────────────────────────────────────────────
# Controls how pre-build hooks behave.
#   "local"   – strip GPG plugin, no version suffix   (default for dev machines)
#   "devel"   – strip GPG plugin, append -nightly_<sha> to version
#   "release" – full pom as-is (GPG signing enabled)
# Override with the ISLANDS_BUILD_MODE environment variable.
BUILD_MODE = os.environ.get("ISLANDS_BUILD_MODE", "local")

# ── Java / sdkman ─────────────────────────────────────────────────────────────
# The sdkman candidate identifier to use for all Maven builds.
# Examples: "24.0.2-tem", "21.0.6-tem", "24.0.2-zulu"
# Set to None to use whatever 'java' is on the current PATH.
JAVA_VERSION = os.environ.get("ISLANDS_JAVA_VERSION", "24.0.2-tem")

# If True and JAVA_VERSION is not installed, automatically install it via sdkman.
AUTO_INSTALL_JAVA = True

# ── Workspace layout ──────────────────────────────────────────────────────────
BUILD_DIR = Path(__file__).resolve().parent   # …/Build
WORKSPACE = BUILD_DIR.parent                  # …/Islands (root)

# ── Output / distribution ─────────────────────────────────────────────────────
OUTPUT_DIR  = WORKSPACE / "output"
MODULES_DIR = OUTPUT_DIR / "modules"

# ── CoffeeLoader runtime config ───────────────────────────────────────────────
COFFEELOADER_RUNTIME_CONFIG = {
    "port": 8080,
    "fileWatcher": True,
    "sources": [str(MODULES_DIR)],
}

# ── Directories that should never be treated as project roots ─────────────────
_SKIP_DIRS = {BUILD_DIR.name, ".idea", ".repo", "output", ".git"}


def scan_projects(workspace: Path = WORKSPACE) -> list[dict]:
    """
    Scan *workspace* for sub-directories that contain a ``project.json`` and
    a ``pom.xml``.  Returns a list of project dicts in topological
    (dependency) order, each with keys:

        name      – human-readable project name
        dir       – absolute Path to project root
        artifact  – Path to the expected fat-jar / jar in target/ (may not
                    exist yet; derived from groupId:artifactId:version in
                    the manifest)

    Order is determined by ``workspace_dependencies``: projects with no
    workspace deps come first; dependent projects follow.
    """
    from hooks import ProjectManifest  # lazy import to avoid circular deps

    # ── 1. Discover all candidate project directories ─────────────────────
    candidates: list[Path] = []
    for entry in sorted(workspace.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name in _SKIP_DIRS or entry.name.startswith("."):
            continue
        if (entry / "project.json").exists() and (entry / "pom.xml").exists():
            candidates.append(entry)

    # ── 2. Load manifests ─────────────────────────────────────────────────
    manifests: dict[str, ProjectManifest] = {}   # artifactId → manifest
    dirs:      dict[str, Path]            = {}   # artifactId → dir

    for d in candidates:
        try:
            m = ProjectManifest.load(d)
        except ValueError as exc:
            import logger as log
            log.warn(f"Skipping {d.name}: {exc}")
            m = None
        if m is not None:
            manifests[m.artifact_id] = m
            dirs[m.artifact_id]      = d

    # ── 3. Topological sort by workspace_dependencies ─────────────────────
    def _artifact_path(m: ProjectManifest) -> Path:
        """
        Derive the primary artifact path from the manifest.
        If the manifest declares ``artifact_name``, that filename is used as-is.
        Otherwise: applications (fat-jars) use ``-jar-with-dependencies`` suffix;
        libraries use the plain jar.
        """
        if m.artifact_name:
            jar_name = m.artifact_name
        elif m.project_type == "application":
            jar_name = f"{m.artifact_id}-{m.version}-jar-with-dependencies.jar"
        else:
            jar_name = f"{m.artifact_id}-{m.version}.jar"
        return dirs[m.artifact_id] / "target" / jar_name

    ordered: list[str]     = []
    visited: set[str]      = set()
    visiting: set[str]     = set()

    def _visit(aid: str) -> None:
        if aid in visited:
            return
        if aid in visiting:
            return   # cycle – skip gracefully
        visiting.add(aid)
        m = manifests.get(aid)
        if m:
            for dep in m.workspace_deps:
                dep_aid = dep.get("artifactId", "")
                if dep_aid in manifests:
                    _visit(dep_aid)
        visiting.discard(aid)
        visited.add(aid)
        ordered.append(aid)

    for aid in manifests:
        _visit(aid)

    # ── 4. Build result list ──────────────────────────────────────────────
    result = []
    for aid in ordered:
        m = manifests[aid]
        result.append({
            "name":     m.name,
            "dir":      dirs[aid],
            "artifact": _artifact_path(m),
        })
    return result


# ── Lazy-evaluated project list (computed once on first access) ───────────────
_projects_cache = None  # type: list[dict] | None


def get_projects() -> list[dict]:
    """Return the workspace-scanned project list (cached after first call)."""
    global _projects_cache
    if _projects_cache is None:
        _projects_cache = scan_projects()
    return _projects_cache


# ── PROJECTS alias (backwards-compat for runner.py / build.py) ───────────────
# Evaluated lazily so that importing config at module-load time never triggers
# the scan.  Code that does  `cfg.PROJECTS`  will get the dynamic list.
class _LazyProjects:
    def __iter__(self):        return iter(get_projects())
    def __len__(self):         return len(get_projects())
    def __getitem__(self, i):  return get_projects()[i]
    def __bool__(self):        return bool(get_projects())


PROJECTS = _LazyProjects()


# ── Git repository roots (also discovered dynamically) ───────────────────────
def get_repos(workspace: Path = WORKSPACE) -> list[dict]:
    """
    Return all git-repo roots in the workspace (workspace root + every
    project sub-directory that is itself a git repo).
    """
    import subprocess
    repos = []

    def _is_git(path: Path) -> bool:
        r = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--git-dir"],
            capture_output=True,
        )
        return r.returncode == 0

    if _is_git(workspace):
        repos.append({"name": workspace.name + " (root)", "dir": workspace})

    for project in get_projects():
        d = Path(project["dir"])
        if d != workspace and _is_git(d):
            repos.append({"name": project["name"], "dir": d})

    return repos


# ── REPOS alias (backwards-compat) ───────────────────────────────────────────
class _LazyRepos:
    def __iter__(self):        return iter(get_repos())
    def __len__(self):         return len(get_repos())
    def __getitem__(self, i):  return get_repos()[i]
    def __bool__(self):        return bool(get_repos())


REPOS = _LazyRepos()
