"""
Source-hash / build-cache system for the Islands build automation.

How it works
------------
For each project a *fingerprint* is computed by hashing:

  1. Every file under ``src/``          (source code, resources)
  2. ``pom.xml``                        (build descriptor)
  3. ``project.json``                   (manifest)
  4. The *resolved version strings* of every workspace dependency declared in
     ``workspace_dependencies``         (so changing ModularKit's version
                                         triggers a rebuild of CoffeeLoader)
  5. The build *mode* string            (local / devel / release)

The fingerprint is a single SHA-256 hex digest stored in a small JSON file
inside  ``<workspace>/.build-cache/<artifactId>.json``.

On the next build the freshly-computed fingerprint is compared against the
stored one.  If they match **and** the artifact (jar) still exists on disk,
the project is skipped.  If ``--clean`` is passed the cache is ignored
entirely and every project is rebuilt.

Public API
----------
  fingerprint(project_dir, manifest, all_manifests, mode) -> str
      Compute the fingerprint for one project (does NOT read/write cache).

  is_up_to_date(project_dir, manifest, all_manifests, mode,
                artifact_path, cache_dir)             -> bool
      Return True iff cached fingerprint == current fingerprint
      AND the artifact jar still exists on disk.

  mark_built(project_dir, manifest, all_manifests, mode,
             cache_dir)                                -> None
      Persist the current fingerprint so subsequent calls to is_up_to_date
      will return True (until sources change again).

  invalidate(artifact_id, cache_dir)                  -> None
      Delete the cached entry for *artifact_id* so the project will rebuild.

  invalidate_dependents(rebuilt_artifact_id, all_manifests, cache_dir) -> None
      Invalidate every project whose workspace_dependencies include
      *rebuilt_artifact_id* (cascade rebuild of downstream projects).

  clear_cache(cache_dir)                              -> None
      Wipe the entire cache directory (called when --clean is given).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from hooks import ProjectManifest

# Files/dirs inside a project root that are irrelevant for the hash
_IGNORE_DIRS  = {"target", ".git", "__pycache__", ".idea", "node_modules"}
_IGNORE_FILES = {".buildconfig-pom.xml", ".DS_Store"}

# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _hash_file(path: Path, h: "hashlib._Hash") -> None:
    """Feed the contents of *path* into *h* in chunks."""
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        # Also hash the relative file name so renames are detected
        h.update(path.name.encode())
    except OSError:
        pass


def _hash_directory(directory: Path, h: "hashlib._Hash") -> None:
    """
    Recursively hash every file under *directory*, skipping
    ``_IGNORE_DIRS`` and ``_IGNORE_FILES``.  Files are visited in
    sorted order so the hash is deterministic.
    """
    if not directory.exists():
        return
    for item in sorted(directory.rglob("*")):
        if item.is_dir():
            if item.name in _IGNORE_DIRS:
                continue
        elif item.is_file():
            # Skip if any parent is in _IGNORE_DIRS
            if any(p.name in _IGNORE_DIRS for p in item.parents):
                continue
            if item.name in _IGNORE_FILES:
                continue
            _hash_file(item, h)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def fingerprint(
    project_dir: Path,
    manifest: "ProjectManifest",
    all_manifests: "dict[str, ProjectManifest]",
    mode: str = "local",
) -> str:
    """
    Compute a SHA-256 fingerprint for *project_dir*.

    Inputs
    ------
    - All files under ``src/``
    - ``pom.xml``
    - ``project.json``
    - Resolved version of each workspace dependency (from *all_manifests*)
    - *mode* string

    Returns a 64-character hex string.
    """
    h = hashlib.sha256()

    # 1. Source tree
    _hash_directory(project_dir / "src", h)

    # 2. Build descriptors
    for fname in ("pom.xml", "project.json"):
        f = project_dir / fname
        if f.exists():
            _hash_file(f, h)

    # 3. Workspace dependency versions (resolved from sibling manifests)
    for dep in sorted(manifest.workspace_deps, key=lambda d: d.get("artifactId", "")):
        aid = dep.get("artifactId", "")
        sibling = all_manifests.get(aid)
        dep_ver = sibling.version if sibling else dep.get("version", "unknown")
        h.update(f"dep:{dep.get('groupId','')}:{aid}:{dep_ver}".encode())

    # 4. Build mode
    h.update(f"mode:{mode}".encode())

    return h.hexdigest()


def _cache_path(artifact_id: str, cache_dir: Path) -> Path:
    return cache_dir / f"{artifact_id}.json"


def _load_cached(artifact_id: str, cache_dir: Path) -> Optional[str]:
    """Return the stored fingerprint hex string, or None if absent/corrupt."""
    p = _cache_path(artifact_id, cache_dir)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("fingerprint")
    except Exception:
        return None


def _save_cached(artifact_id: str, fingerprint_hex: str, cache_dir: Path) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    _cache_path(artifact_id, cache_dir).write_text(
        json.dumps({"fingerprint": fingerprint_hex}, indent=2) + "\n",
        encoding="utf-8",
    )


def is_up_to_date(
    project_dir: Path,
    manifest: "ProjectManifest",
    all_manifests: "dict[str, ProjectManifest]",
    mode: str,
    artifact_path: Path,
    cache_dir: Path,
) -> bool:
    """
    Return True only if:
      - A cached fingerprint exists for this project, AND
      - It matches the freshly-computed fingerprint, AND
      - The artifact jar file actually exists on disk.

    Any mismatch → False (rebuild required).
    """
    if not artifact_path.exists():
        return False

    stored = _load_cached(manifest.artifact_id, cache_dir)
    if stored is None:
        return False

    current = fingerprint(project_dir, manifest, all_manifests, mode)
    return current == stored


def mark_built(
    project_dir: Path,
    manifest: "ProjectManifest",
    all_manifests: "dict[str, ProjectManifest]",
    mode: str,
    cache_dir: Path,
) -> None:
    """
    Persist the current fingerprint for *manifest.artifact_id* so that the
    next call to :func:`is_up_to_date` returns True (until sources change).
    """
    fp = fingerprint(project_dir, manifest, all_manifests, mode)
    _save_cached(manifest.artifact_id, fp, cache_dir)


def invalidate(artifact_id: str, cache_dir: Path) -> None:
    """Delete the cached fingerprint for *artifact_id*."""
    p = _cache_path(artifact_id, cache_dir)
    if p.exists():
        p.unlink()


def invalidate_dependents(
    rebuilt_artifact_id: str,
    all_manifests: "dict[str, ProjectManifest]",
    cache_dir: Path,
) -> list[str]:
    """
    After *rebuilt_artifact_id* was just built, invalidate every other
    project that lists it as a workspace dependency — so they will rebuild
    on the next pass and pick up the new library version.

    Returns the list of artifact IDs that were invalidated.
    """
    invalidated: list[str] = []
    for aid, m in all_manifests.items():
        if aid == rebuilt_artifact_id:
            continue
        dep_ids = {d.get("artifactId") for d in m.workspace_deps}
        if rebuilt_artifact_id in dep_ids:
            invalidate(aid, cache_dir)
            invalidated.append(aid)
    return invalidated


def clear_cache(cache_dir: Path) -> None:
    """Wipe the entire cache directory (used when --clean is given)."""
    import shutil
    if cache_dir.exists():
        shutil.rmtree(cache_dir)


def scan_changed(
    projects: list,
    all_manifests: "dict[str, ProjectManifest]",
    mode: str,
    cache_dir: Path,
) -> list[str]:
    """
    Return the artifact IDs of every project whose source fingerprint
    differs from the cached value (or has no cached value at all).

    *projects* is the list returned by ``cfg.get_projects()``.
    Does NOT check whether the artifact jar exists — that is intentionally
    left to the caller so the watcher can distinguish "stale" from "missing".
    """
    stale: list[str] = []
    for p in projects:
        from hooks import ProjectManifest  # lazy
        manifest = ProjectManifest.load(Path(p["dir"]))
        if manifest is None:
            continue
        stored = _load_cached(manifest.artifact_id, cache_dir)
        current = fingerprint(Path(p["dir"]), manifest, all_manifests, mode)
        if stored != current:
            stale.append(manifest.artifact_id)
    return stale


