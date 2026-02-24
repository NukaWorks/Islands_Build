"""
Universal pre/post-build hook system for the Islands build automation.

────────────────────────────────────────────────────────────────────────────
Project Manifest  (project.json)
────────────────────────────────────────────────────────────────────────────
Every project root may contain a ``project.json`` file that declares its
identity and build settings::

    {
      "name":        "ModularKit",
      "groupId":     "works.nuka",
      "artifactId":  "ModularKit",
      "version":     "1.8.3",
      "type":        "library",          // "library" | "application"
      "description": "...",
      "build": {
        "strip_gpg_unless_release": true,
        "nightly_suffix_on_devel":  true
      },
      "workspace_dependencies": [
        {"groupId": "works.nuka", "artifactId": "ModularKit"}
      ]
    }

``workspace_dependencies`` lists other workspace projects this project
depends on.  Before each build the universal hook resolves their current
versions from their own ``project.json`` and patches the matching
``<dependency>`` blocks in ``pom.xml`` so every project always uses the
versions declared in their sibling manifests.

Projects are discovered automatically by scanning the workspace root for
sub-directories that contain both ``project.json`` and ``pom.xml``.  No
project list needs to be hardcoded anywhere.

────────────────────────────────────────────────────────────────────────────
Hook system
────────────────────────────────────────────────────────────────────────────
A **Hook** is any callable ``(HookContext) -> HookResult``.

The universal hook ``universal_prebuild`` is applied automatically to every
discovered project.  It does:
  1. Load ``project.json`` (if present).
  2. Collect version map of all workspace projects (via scan).
  3. Patch ``pom.xml``:
       - ``<groupId>``, ``<artifactId>``, ``<version>`` of the project itself
       - ``<version>`` of every workspace dependency listed in the manifest
       - Optionally append ``-nightly_<sha>`` to version (devel mode)
       - Optionally strip the GPG-sign plugin (non-release mode)
  4. Write the patched XML to ``.buildconfig-pom.xml`` so the original
     ``pom.xml`` is never touched.
  5. Return ``pom_override`` pointing at the generated file.

If no ``project.json`` exists, the hook is a no-op (returns success,
no pom override) so projects without manifests build normally.
"""
from __future__ import annotations

import json
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from xml.dom import minidom

import logger as log

# ── Maven XML namespace ────────────────────────────────────────────────────
_MVN_NS  = "http://maven.apache.org/POM/4.0.0"
_NS_MAP  = {"m": _MVN_NS}
ET.register_namespace("",        _MVN_NS)
ET.register_namespace("xsi",     "http://www.w3.org/2001/XMLSchema-instance")


# ══════════════════════════════════════════════════════════════════════════════
# Public data types
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class HookContext:
    """
    Runtime context passed to every hook invocation.

    project_name  – human-readable name (e.g. "ModularKit")
    project_dir   – absolute Path to project root
    mode          – "local" | "devel" | "release"
    commit_id     – short HEAD SHA (auto-resolved if blank)
    verbose       – stream hook output to terminal
    workspace_dir – workspace root (resolved from config at runtime)
    extra         – free-form dict for hook-specific parameters
    """
    project_name:  str
    project_dir:   Path
    mode:          str  = "local"
    commit_id:     str  = ""
    verbose:       bool = False
    workspace_dir: Optional[Path] = None
    extra:         dict = field(default_factory=dict)


@dataclass
class HookResult:
    """
    Return value from a hook callable.

    success          – False → abort the build for this project
    pom_override     – Path to use instead of pom.xml (via ``-f``)
    extra_maven_args – extra CLI args appended to Maven invocation
    message          – human-readable status (logged automatically)
    """
    success:          bool            = True
    pom_override:     Optional[Path]  = None
    extra_maven_args: list            = field(default_factory=list)
    message:          str             = ""


Hook = Callable[[HookContext], HookResult]


# ══════════════════════════════════════════════════════════════════════════════
# ProjectManifest
# ══════════════════════════════════════════════════════════════════════════════

_MANIFEST_FILE = "project.json"

# Valid project types
PROJECT_TYPES = ("library", "application")


@dataclass
class ProjectManifest:
    """
    In-memory representation of a project's ``project.json`` manifest.

    This is the single source of truth for a project's identity inside the
    workspace.  The build hook reads it, patches the ``pom.xml``, and syncs
    dependency versions from sibling manifests.
    """
    path:         Path           # absolute path to project.json
    name:         str
    group_id:     str
    artifact_id:  str
    version:      str
    project_type: str            # "library" | "application"
    description:  str            = ""
    build:        dict           = field(default_factory=dict)
    workspace_deps: list[dict]   = field(default_factory=list)
    artifact_name:  str          = ""   # optional override for the output jar filename
    module:         dict         = field(default_factory=dict)  # ModularKit module descriptor

    # ── factories ──────────────────────────────────────────────────────────

    @classmethod
    def load(cls, project_dir: Path) -> Optional["ProjectManifest"]:
        """
        Load ``project.json`` from *project_dir*.
        Returns ``None`` if the file does not exist.
        Raises ``ValueError`` on malformed JSON or missing required fields.
        """
        manifest_path = project_dir / _MANIFEST_FILE
        if not manifest_path.exists():
            return None
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed {manifest_path}: {exc}") from exc

        for key in ("name", "groupId", "artifactId", "version", "type"):
            if key not in data:
                raise ValueError(f"{manifest_path}: missing required field '{key}'")

        ptype = data["type"]
        if ptype not in PROJECT_TYPES:
            raise ValueError(
                f"{manifest_path}: 'type' must be one of {PROJECT_TYPES}, got '{ptype}'"
            )

        return cls(
            path         = manifest_path,
            name         = data["name"],
            group_id     = data["groupId"],
            artifact_id  = data["artifactId"],
            version      = data["version"],
            project_type = ptype,
            description  = data.get("description", ""),
            build        = data.get("build", {}),
            workspace_deps = data.get("workspace_dependencies", []),
            artifact_name  = data.get("artifact_name", ""),
            module         = data.get("module", {}),
        )

    @classmethod
    def load_all(cls, workspace_dir: Path, project_dirs: list[Path]) -> dict[str, "ProjectManifest"]:
        """
        Load all manifests from *project_dirs*.
        Returns ``{artifactId: ProjectManifest}`` for every project that has
        a ``project.json``.
        """
        result: dict[str, ProjectManifest] = {}
        for d in project_dirs:
            try:
                m = cls.load(d)
            except ValueError as exc:
                log.warn(str(exc))
                m = None
            if m is not None:
                result[m.artifact_id] = m
        return result

    # ── persistence ────────────────────────────────────────────────────────

    def save(self) -> None:
        """Write the manifest back to its ``project.json`` file."""
        data = {
            "name":        self.name,
            "groupId":     self.group_id,
            "artifactId":  self.artifact_id,
            "version":     self.version,
            "type":        self.project_type,
            "description": self.description,
            "build":       self.build,
            "workspace_dependencies": self.workspace_deps,
        }
        if self.artifact_name:
            data["artifact_name"] = self.artifact_name
        if self.module:
            data["module"] = self.module
        self.path.write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8"
        )

    # ── helpers ────────────────────────────────────────────────────────────

    def is_library(self) -> bool:
        return self.project_type == "library"

    def is_application(self) -> bool:
        return self.project_type == "application"

    def is_module(self) -> bool:
        """Return True if this project has a ModularKit module descriptor."""
        return bool(self.module)

    def effective_version(self, mode: str, commit_id: str) -> str:
        """
        Return the version string to embed in the generated pom.

        - ``"release"`` → ``self.version`` unchanged
        - ``"devel"``   → ``self.version-nightly_<commit_id>``  (if flag set)
        - ``"local"``   → ``self.version`` unchanged
        """
        if (
            mode == "devel"
            and self.build.get("nightly_suffix_on_devel", False)
            and commit_id
        ):
            return f"{self.version}-nightly_{commit_id}"
        return self.version

    def __repr__(self) -> str:
        return (
            f"ProjectManifest({self.group_id}:{self.artifact_id}:{self.version}"
            f"  [{self.project_type}])"
        )


# ══════════════════════════════════════════════════════════════════════════════
# POM patcher
# ══════════════════════════════════════════════════════════════════════════════

def _pom_tag(local: str) -> str:
    return f"{{{_MVN_NS}}}{local}"


def _find_or_none(element: ET.Element, *path: str) -> Optional[ET.Element]:
    """Walk a chain of tag names, return the last element or None."""
    cur = element
    for tag in path:
        nxt = cur.find(_pom_tag(tag))
        if nxt is None:
            return None
        cur = nxt
    return cur


def _set_text(parent: ET.Element, tag: str, value: str) -> None:
    """Find ``<tag>`` under *parent* and set its text; no-op if not found."""
    el = parent.find(_pom_tag(tag))
    if el is not None:
        el.text = value


def _pretty_xml(root: ET.Element) -> str:
    """Return indented XML string, stripping the minidom declaration line."""
    raw  = ET.tostring(root, encoding="unicode", xml_declaration=False)
    # Re-inject the original XML declaration so the file is well-formed
    raw  = '<?xml version="1.0" encoding="UTF-8"?>\n' + raw
    dom  = minidom.parseString(raw.encode("utf-8"))
    lines = dom.toprettyxml(indent="    ", encoding=None).splitlines()
    # minidom re-adds a declaration; drop it (we already added one above)
    lines = [line_ for line_ in lines if not line_.startswith("<?xml")]
    result = '<?xml version="1.0" encoding="UTF-8"?>\n' + "\n".join(lines) + "\n"
    # Strip ns0: artifacts that ET occasionally emits
    result = result.replace("ns0:", "").replace(":ns0", "")
    return result


def patch_pom(
    pom_path: Path,
    manifest: ProjectManifest,
    all_manifests: dict[str, "ProjectManifest"],
    *,
    mode: str = "local",
    commit_id: str = "",
    dest: Optional[Path] = None,
) -> Path:
    """
    Read *pom_path*, apply manifest-driven patches, write to *dest*.

    Patches applied
    ---------------
    1. Project ``<groupId>``, ``<artifactId>``, ``<version>`` from manifest.
    2. For every ``workspace_dependencies`` entry: find the matching
       ``<dependency>`` block and update its ``<version>`` to the resolved
       version from *all_manifests*.
    3. If mode != "release" and ``strip_gpg_unless_release`` is True:
       remove the maven-gpg-plugin execution block.

    Returns the path of the written file (*dest* or
    ``pom_path.parent / ".buildconfig-pom.xml"``).
    """
    dest = dest or (pom_path.parent / ".buildconfig-pom.xml")

    tree = ET.parse(str(pom_path))
    root = tree.getroot()

    effective_ver = manifest.effective_version(mode, commit_id)

    # ── 1. Patch project identity ─────────────────────────────────────────
    _set_text(root, "groupId",    manifest.group_id)
    _set_text(root, "artifactId", manifest.artifact_id)
    _set_text(root, "version",    effective_ver)

    # ── 2. Sync workspace dependency versions ─────────────────────────────
    deps_root = root.find(_pom_tag("dependencies"))
    if deps_root is not None:
        for dep_el in deps_root.findall(_pom_tag("dependency")):
            dep_group    = (dep_el.findtext(_pom_tag("groupId"))    or "").strip()
            dep_artifact = (dep_el.findtext(_pom_tag("artifactId")) or "").strip()
            # Check against workspace_deps declared in the manifest
            for wdep in manifest.workspace_deps:
                if (
                    wdep.get("groupId")    == dep_group
                    and wdep.get("artifactId") == dep_artifact
                ):
                    sibling = all_manifests.get(dep_artifact)
                    if sibling:
                        resolved = sibling.effective_version(mode, commit_id)
                        _set_text(dep_el, "version", resolved)
                        log.info(
                            f"  sync dep {dep_group}:{dep_artifact} → {resolved}"
                        )
                    break

    # ── 3. Strip GPG plugin (non-release builds) ──────────────────────────
    if (
        mode != "release"
        and manifest.build.get("strip_gpg_unless_release", False)
    ):
        build_el   = root.find(_pom_tag("build"))
        plugins_el = _find_or_none(root, "build", "plugins") if build_el is not None else None
        if plugins_el is not None:
            for plugin_el in list(plugins_el.findall(_pom_tag("plugin"))):
                aid = (plugin_el.findtext(_pom_tag("artifactId")) or "").strip()
                if aid == "maven-gpg-plugin":
                    plugins_el.remove(plugin_el)
                    log.info("  stripped maven-gpg-plugin (non-release build)")

    dest.write_text(_pretty_xml(root), encoding="utf-8")
    return dest


# ══════════════════════════════════════════════════════════════════════════════
# Universal hook
# ══════════════════════════════════════════════════════════════════════════════

def universal_prebuild(ctx: HookContext) -> HookResult:
    """
    Universal pre-build hook — works for every project in the workspace.

    Steps
    -----
    1. Load ``project.json`` from the project directory.
       → If absent, return success with no pom override (build proceeds
         normally with the stock pom.xml).
    2. Collect all sibling project manifests (for version syncing).
    3. Patch ``pom.xml`` via :func:`patch_pom`:
         - project identity (groupId / artifactId / version)
         - workspace dependency versions
         - GPG plugin strip (library + non-release)
         - nightly version suffix (devel mode, if opted-in)
    4. Return ``pom_override`` pointing at ``.buildconfig-pom.xml``.
    """
    # ── load this project's manifest ──────────────────────────────────────
    try:
        manifest = ProjectManifest.load(ctx.project_dir)
    except ValueError as exc:
        return HookResult(success=False, message=str(exc))

    if manifest is None:
        # No project.json – skip silently, build with stock pom
        log.info(f"[{ctx.project_name}] no project.json found – skipping pom patch")
        return HookResult(success=True)

    log.info(
        f"[{ctx.project_name}] universal_prebuild  "
        f"mode={ctx.mode}  type={manifest.project_type}  "
        f"version={manifest.effective_version(ctx.mode, ctx.commit_id)}"
    )

    # ── collect all workspace manifests for dep-version syncing ───────────
    workspace = ctx.workspace_dir
    if workspace is None:
        # Fall back: try to import config lazily
        try:
            import config as _cfg
            workspace = _cfg.WORKSPACE
        except ImportError:
            workspace = ctx.project_dir.parent

    sibling_dirs = [
        d for d in workspace.iterdir()
        if d.is_dir() and (d / _MANIFEST_FILE).exists() and d != ctx.project_dir
    ]
    all_manifests = ProjectManifest.load_all(workspace, sibling_dirs)
    # Include self so cross-refs work
    all_manifests[manifest.artifact_id] = manifest

    # ── resolve commit id ──────────────────────────────────────────────────
    commit = ctx.commit_id or _get_commit_id(ctx.project_dir) or "0000000"

    # ── patch pom ─────────────────────────────────────────────────────────
    pom_path = ctx.project_dir / "pom.xml"
    if not pom_path.exists():
        return HookResult(
            success=False,
            message=f"pom.xml not found in {ctx.project_dir}",
        )

    try:
        dest = patch_pom(
            pom_path,
            manifest,
            all_manifests,
            mode=ctx.mode,
            commit_id=commit,
        )
    except Exception as exc:
        return HookResult(success=False, message=f"pom patching failed: {exc}")

    log.success(f"[{ctx.project_name}] pom patched → {dest.name}")

    # ── run manifest-declared named hooks ────────────────────────────────
    named = _resolve_named_hooks(manifest, "pre_build")
    for hook_fn in named:
        hook_name = getattr(hook_fn, "__name__", repr(hook_fn))
        log.info(f"[{ctx.project_name}] pre_build (manifest) → {hook_name}")
        try:
            named_result = hook_fn(ctx)
        except Exception as exc:
            return HookResult(success=False, message=f"hook '{hook_name}' raised: {exc}")
        if named_result.message:
            (log.info if named_result.success else log.error)(f"  → {named_result.message}")
        if not named_result.success:
            return HookResult(success=False, message=f"hook '{hook_name}' failed")

    return HookResult(
        success=True,
        pom_override=dest,
        message=f"pom override: {dest}",
    )


# ══════════════════════════════════════════════════════════════════════════════
# Manifest-change sync  (patch pom.xml in-place after project.json edits)
# ══════════════════════════════════════════════════════════════════════════════

def sync_module_json(manifest: "ProjectManifest") -> bool:
    """
    Write ``src/main/resources/module.json`` from the ``module`` block of
    *manifest*, injecting the current ``version`` so it stays in sync.

    Does nothing and returns True if the manifest has no ``module`` block.
    Returns False on write failure.
    """
    if not manifest.module:
        return True

    resources_dir = manifest.path.parent / "src" / "main" / "resources"
    module_json_path = resources_dir / "module.json"

    if not resources_dir.exists():
        log.warn(f"sync_module_json: resources dir not found: {resources_dir}")
        return False

    data = dict(manifest.module)          # shallow copy
    data["version"] = manifest.version    # keep version in sync

    try:
        import json as _json
        module_json_path.write_text(
            _json.dumps(data, indent=2) + "\n", encoding="utf-8"
        )
        log.success(f"  module.json synced → {module_json_path}")
        return True
    except Exception as exc:
        log.error(f"sync_module_json failed for {manifest.name}: {exc}")
        return False


def remove_pom_dependency(
    project_dir: Path,
    group_id: Optional[str],
    artifact_id: str,
) -> bool:
    """
    Remove a ``<dependency>`` block from ``pom.xml`` in *project_dir* whose
    ``<artifactId>`` matches *artifact_id* (and optionally *group_id*).

    Writes the result back to ``pom.xml`` in-place.
    Returns ``True`` if a dependency was removed, ``False`` otherwise.
    """
    pom_path = project_dir / "pom.xml"
    if not pom_path.exists():
        return False

    try:
        tree = ET.parse(str(pom_path))
        root = tree.getroot()

        deps_root = root.find(_pom_tag("dependencies"))
        if deps_root is None:
            return False

        removed = False
        for dep_el in list(deps_root.findall(_pom_tag("dependency"))):
            dep_aid = (dep_el.findtext(_pom_tag("artifactId")) or "").strip()
            dep_gid = (dep_el.findtext(_pom_tag("groupId"))    or "").strip()
            if dep_aid == artifact_id and (group_id is None or dep_gid == group_id):
                deps_root.remove(dep_el)
                removed = True
                log.info(f"  removed <dependency> {dep_gid}:{dep_aid} from pom.xml")

        if removed:
            pom_path.write_text(_pretty_xml(root), encoding="utf-8")
        return removed
    except Exception as exc:
        log.error(f"remove_pom_dependency failed for {project_dir.name}: {exc}")
        return False


def sync_pom_versions(
    project_dir: Path,
    all_manifests: dict[str, "ProjectManifest"],
    *,
    mode: str = "local",
    commit_id: str = "",
) -> bool:
    """
    Patch ``pom.xml`` **in-place** (overwriting the real file, not a build
    override) so that version numbers stay consistent with ``project.json``.

    Unlike :func:`patch_pom`, this writes directly to ``pom.xml`` so the
    canonical source of truth is always up-to-date after manifest edits.

    Returns ``True`` on success, ``False`` if the pom is missing or an error
    occurs.
    """
    manifest = all_manifests.get(
        next(
            (aid for aid, m in all_manifests.items() if m.path.parent == project_dir),
            None,
        )
    )
    if manifest is None:
        # Try loading directly
        try:
            manifest = ProjectManifest.load(project_dir)
        except ValueError:
            manifest = None
    if manifest is None:
        return False

    pom_path = project_dir / "pom.xml"
    if not pom_path.exists():
        return False

    try:
        patch_pom(
            pom_path,
            manifest,
            all_manifests,
            mode=mode,
            commit_id=commit_id,
            dest=pom_path,          # overwrite the real pom.xml
        )
        return True
    except Exception as exc:
        log.error(f"sync_pom_versions failed for {project_dir.name}: {exc}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Root POM sync  (keeps <modules> in the workspace root pom.xml up-to-date)
# ══════════════════════════════════════════════════════════════════════════════

def sync_root_pom(
    workspace_dir: Path,
    all_manifests: Optional[dict[str, "ProjectManifest"]] = None,
    *,
    root_pom_path: Optional[Path] = None,
) -> bool:
    """
    Regenerate the ``<modules>`` section of the workspace root ``pom.xml``
    so that it always reflects the projects discovered via ``project.json``
    files, listed in topological (dependency) order.

    The function:

    1. Discovers / reuses *all_manifests* (``{artifactId: ProjectManifest}``).
    2. Performs a topological sort of the projects by their
       ``workspace_dependencies`` — projects with no deps come first.
    3. Reads the existing root ``pom.xml``, replaces **only** the
       ``<modules>`` block (all other content is preserved).
    4. Writes the result back to ``root_pom_path``
       (default: ``workspace_dir / "pom.xml"``).

    Returns ``True`` on success, ``False`` on any error.

    Parameters
    ----------
    workspace_dir:
        Absolute path to the workspace root directory.
    all_manifests:
        Optional pre-built manifest map.  When *None* the function scans
        *workspace_dir* for ``project.json`` files automatically.
    root_pom_path:
        Path to the root ``pom.xml``.  Defaults to
        ``workspace_dir / "pom.xml"``.
    """
    root_pom_path = root_pom_path or (workspace_dir / "pom.xml")
    if not root_pom_path.exists():
        log.warn(f"sync_root_pom: root pom not found at {root_pom_path}")
        return False

    # ── 1. Collect manifests ──────────────────────────────────────────────
    if all_manifests is None:
        all_manifests = {}
        for entry in sorted(workspace_dir.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name.startswith(".") or entry.name in {"Build", "output"}:
                continue
            try:
                m = ProjectManifest.load(entry)
            except ValueError:
                m = None
            if m is not None:
                all_manifests[m.artifact_id] = m

    if not all_manifests:
        log.warn("sync_root_pom: no project manifests found – root pom unchanged")
        return True

    # ── 2. Topological sort ───────────────────────────────────────────────
    ordered: list[str] = []
    visited: set[str]  = set()
    visiting: set[str] = set()

    def _visit(aid: str) -> None:
        if aid in visited:
            return
        if aid in visiting:
            return   # cycle – skip gracefully
        visiting.add(aid)
        m = all_manifests.get(aid)
        if m:
            for dep in m.workspace_deps:
                dep_aid = dep.get("artifactId", "")
                if dep_aid in all_manifests:
                    _visit(dep_aid)
        visiting.discard(aid)
        visited.add(aid)
        ordered.append(aid)

    for aid in sorted(all_manifests.keys()):
        _visit(aid)

    # Module names are the sub-directory names (manifest path parent basename)
    module_names = [
        all_manifests[aid].path.parent.name for aid in ordered
    ]

    # ── 3. Patch the root pom.xml with a regex-based replace ─────────────
    # We use a text-level replacement to avoid the whitespace noise that
    # minidom round-tripping introduces into an already-formatted file.
    import re as _re

    try:
        original = root_pom_path.read_text(encoding="utf-8")

        # Detect indentation from the <module> tag — ignore blank-only lines
        indent_match = _re.search(r'^([ \t]+)<module>', original, _re.MULTILINE)
        module_indent = indent_match.group(1) if indent_match else "        "
        # The <modules> container is one indent level up (4 spaces)
        outer_indent = module_indent[:-4] if len(module_indent) >= 4 else "    "

        new_modules_block = (
            f"{outer_indent}<modules>\n"
            + "".join(f"{module_indent}<module>{n}</module>\n" for n in module_names)
            + f"{outer_indent}</modules>"
        )

        # Replace an existing <modules>…</modules> block (including multiline,
        # allowing for any leading whitespace on the opening tag line)
        new_content, n_subs = _re.subn(
            r'[ \t]*<modules>.*?</modules>',
            new_modules_block,
            original,
            flags=_re.DOTALL,
        )

        if n_subs == 0:
            # No existing block — insert after <packaging> or <name> or <version>
            for anchor_tag in ("packaging", "name", "version", "artifactId"):
                pattern = rf'([ \t]*<{anchor_tag}>[^<]*</{anchor_tag}>)'
                new_content, n_subs = _re.subn(
                    pattern,
                    rf'\1\n{new_modules_block}',
                    original,
                    count=1,
                )
                if n_subs:
                    break

        if n_subs == 0:
            log.warn("sync_root_pom: could not locate insertion point — appending before </project>")
            new_content = original.replace(
                "</project>",
                f"{new_modules_block}\n</project>",
                1,
            )

        root_pom_path.write_text(new_content, encoding="utf-8")
        log.success(
            "root pom.xml <modules> synced: "
            + " → ".join(module_names)
        )
        return True
    except Exception as exc:
        log.error(f"sync_root_pom failed: {exc}")
        return False


# ── Backwards-compat alias ─────────────────────────────────────────────────
def modularkit_prebuild(ctx: HookContext) -> HookResult:
    """
    Legacy alias kept for config compatibility.
    Now delegates to ``universal_prebuild``.
    """
    return universal_prebuild(ctx)


# ══════════════════════════════════════════════════════════════════════════════
# Named hooks  (declared in project.json → "build" → "hooks" → "pre_build")
# ══════════════════════════════════════════════════════════════════════════════

def copy_config_prebuild(ctx: HookContext) -> HookResult:
    """
    Pre-build hook: copy the project's ``config.json`` into the workspace
    output directory, overwriting the ``sources`` field so it always points
    at the runtime ``output/modules`` directory.

    Configured via the project's ``build.hooks.copy_config`` block::

        "build": {
          "hooks": {
            "pre_build": ["copy_config"]
          },
          "copy_config": {
            "src":  "config.json"   // relative to project root (default)
          }
        }

    The destination is always ``<workspace>/output/config.json``.
    The ``sources`` field is overwritten with ``["<workspace>/output/modules"]``.
    """
    import fs as _fs

    # Resolve workspace
    workspace = ctx.workspace_dir
    if workspace is None:
        try:
            import config as _cfg
            workspace = _cfg.WORKSPACE
        except ImportError:
            workspace = ctx.project_dir.parent

    # Read optional per-project copy_config settings from build block
    try:
        manifest = ProjectManifest.load(ctx.project_dir)
    except ValueError as exc:
        return HookResult(success=False, message=str(exc))

    copy_cfg: dict = {}
    if manifest is not None:
        copy_cfg = manifest.build.get("copy_config", {})

    src_rel = copy_cfg.get("src", "config.json")
    src = ctx.project_dir / src_rel
    dst = workspace / "output" / "config.json"
    modules_dir = str(workspace / "output" / "modules")

    ok = _fs.copy_config(src, dst, sources_override=[modules_dir])
    if not ok:
        return HookResult(success=False, message=f"copy_config: failed to copy {src} → {dst}")

    return HookResult(success=True, message=f"copy_config: {src.name} → {dst}")


# ── Registry: hook name (as declared in project.json) → callable ──────────
NAMED_HOOKS: dict[str, Hook] = {
    "copy_config": copy_config_prebuild,
}


def _resolve_named_hooks(manifest: "ProjectManifest", phase: str) -> list[Hook]:
    """
    Return the list of Hook callables declared in *manifest*'s
    ``build.hooks.<phase>`` array, resolving names via :data:`NAMED_HOOKS`.

    Unknown names are warned and skipped.
    """
    names: list[str] = []
    if manifest is not None:
        names = manifest.build.get("hooks", {}).get(phase, [])

    hooks_out: list[Hook] = []
    for name in names:
        fn = NAMED_HOOKS.get(name)
        if fn is None:
            log.warn(f"Unknown hook name '{name}' declared in project.json — skipped.")
        else:
            hooks_out.append(fn)
    return hooks_out


# ══════════════════════════════════════════════════════════════════════════════
# Hook runner
# ══════════════════════════════════════════════════════════════════════════════

def run_hooks(
    phase: str,
    hooks: list[Hook],
    ctx: HookContext,
) -> tuple[bool, Optional[Path], list[str]]:
    """
    Execute all hooks for *phase* (``"pre_build"`` or ``"post_build"``).

    Returns ``(ok, pom_override, extra_maven_args)``.
    """
    if not hooks:
        return True, None, []

    pom_override:     Optional[Path] = None
    extra_maven_args: list[str]      = []

    for hook in hooks:
        hook_name = getattr(hook, "__name__", repr(hook))
        log.info(f"[{ctx.project_name}] {phase} → {hook_name}")
        try:
            result: HookResult = hook(ctx)
        except Exception as exc:
            log.error(f"[{ctx.project_name}] hook '{hook_name}' raised: {exc}")
            return False, pom_override, extra_maven_args

        if result.message:
            (log.info if result.success else log.error)(f"  → {result.message}")

        if not result.success:
            log.error(
                f"[{ctx.project_name}] {phase} hook '{hook_name}' failed – aborting."
            )
            return False, pom_override, extra_maven_args

        if result.pom_override is not None:
            pom_override = result.pom_override
        extra_maven_args.extend(result.extra_maven_args)

    return True, pom_override, extra_maven_args


# ── Context factory ────────────────────────────────────────────────────────

def build_hook_context(
    project: dict,
    *,
    mode: str = "local",
    commit_id: str = "",
    verbose: bool = False,
    workspace_dir: Optional[Path] = None,
) -> HookContext:
    """Build a ``HookContext`` from a project dict (as used in config.PROJECTS)."""
    project_dir = Path(project["dir"])
    if workspace_dir is None:
        try:
            import config as _cfg
            workspace_dir = _cfg.WORKSPACE
        except ImportError:
            workspace_dir = project_dir.parent
    return HookContext(
        project_name  = project["name"],
        project_dir   = project_dir,
        mode          = mode,
        commit_id     = commit_id or _get_commit_id(project_dir),
        verbose       = verbose,
        workspace_dir = workspace_dir,
        extra         = project.get("hook_extra", {}),
    )


# ── Internal helpers ───────────────────────────────────────────────────────

def _get_commit_id(project_dir: Path) -> str:
    """Return the short HEAD SHA for *project_dir*, or empty string."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except FileNotFoundError:
        return ""

