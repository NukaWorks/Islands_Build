"""
High-level run configurations:
  - build_all_and_test  : build every project, run tests for each
  - build_and_run_islands: build all, assemble output dir, launch CoffeeLoader
"""
import signal
import subprocess
from pathlib import Path
from typing import Dict, Optional

import config as cfg
import fs
import hasher as hashermod
import hooks as hooksmod
import logger as log
import maven
import sdkman


# ─────────────────────────────────────────────────────────────────────────────
# Java / sdkman helpers
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_env(java_version: Optional[str] = None) -> Optional[Dict[str, str]]:
    """
    Resolve JAVA_HOME for *java_version* (defaults to cfg.JAVA_VERSION).
    Returns a full env dict suitable for subprocess calls, or None on failure.
    If cfg.JAVA_VERSION is None, returns None (use ambient PATH).
    """
    version = java_version or cfg.JAVA_VERSION
    if not version:
        log.info("No JAVA_VERSION configured – using ambient java on PATH.")
        return None

    java_home = sdkman.ensure_java(version, auto_install=cfg.AUTO_INSTALL_JAVA)
    if java_home is None:
        return None                   # error already logged by sdkman module

    return sdkman.build_env(java_home)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_project(project: dict, *, skip_tests: bool, verbose: bool) -> bool:
    return maven.build_project(
        project["name"],
        project["dir"],
        skip_tests=skip_tests,
        verbose=verbose,
    )


def _check_artifact(project: dict) -> bool:
    art = project.get("artifact")
    if art and not Path(art).exists():
        log.warn(f"Expected artifact not found: {art}")
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Run config 1 – build + test every project individually
# ─────────────────────────────────────────────────────────────────────────────

def build_all_and_test(*, verbose: bool = False, java_version: Optional[str] = None) -> bool:
    """
    Build every project in dependency order, running tests for each.
    Returns True only if all builds succeed.
    """
    projects = cfg.get_projects()
    log.banner(
        "Build All & Test",
        f"{len(projects)} project(s) in dependency order  (tests enabled)",
    )

    env = _resolve_env(java_version)
    if env is None and cfg.JAVA_VERSION:
        return False   # resolution failed

    total = len(projects)
    for i, project in enumerate(projects, 1):
        log.step(i, total, project["name"])
        ok = maven.build_project(
            project["name"],
            project["dir"],
            skip_tests=False,
            verbose=verbose,
            env=env,
        )
        if not ok:
            log.error(f"Stopping: {project['name']} build failed.")
            return False
        _check_artifact(project)

    log.banner("All projects built and tested successfully!")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Run config 2 – build all then launch Islands via CoffeeLoader
# ─────────────────────────────────────────────────────────────────────────────

def _assemble_output(*, clean: bool = False) -> bool:
    """
    Assemble the output directory from all discovered projects.

    Routing rules (in priority order):
      - Has a ``module`` block in project.json  → output/modules/   (ModularKit module)
      - application, no module block            → output/           (launcher / fat-jar)
      - library                                 → output/           (classpath dep)

    The first application project that is NOT a module is treated as the launcher.
    """
    log.section("Assembling output directory")

    if clean:
        fs.clean_output(cfg.OUTPUT_DIR)
    else:
        fs.ensure_dir(cfg.OUTPUT_DIR)
        fs.ensure_dir(cfg.MODULES_DIR)

    projects = cfg.get_projects()
    launcher_jar: Optional[Path] = None

    for project in projects:
        art = project.get("artifact")
        if not art:
            continue
        art = Path(art)

        # Load manifest to check module block and project type
        m = None
        try:
            from hooks import ProjectManifest
            m = ProjectManifest.load(Path(project["dir"]))
        except Exception:
            pass

        if m and m.is_module():
            # ModularKit module → output/modules/
            dest = cfg.MODULES_DIR / art.name
            ok = fs.copy_artifact(art, dest)
            if not ok:
                return False
        else:
            # Launcher or library → output/
            dest = cfg.OUTPUT_DIR / art.name
            ok = fs.copy_artifact(art, dest)
            if not ok:
                return False
            if m and m.is_application() and launcher_jar is None:
                launcher_jar = dest

    # Write CoffeeLoader-compatible config.json
    runtime_cfg = {
        "port":        cfg.COFFEELOADER_RUNTIME_CONFIG["port"],
        "fileWatcher": cfg.COFFEELOADER_RUNTIME_CONFIG["fileWatcher"],
        "sources":     [str(cfg.MODULES_DIR)],
    }
    fs.write_json(cfg.OUTPUT_DIR / "config.json", runtime_cfg)
    log.info(f"Module source: {cfg.MODULES_DIR}")
    return True


def build_and_run_islands(
    *,
    skip_tests: bool = True,
    clean_output: bool = True,
    fast_build: bool = False,
    clean: bool = False,
    verbose: bool = False,
    java_opts: Optional[str] = None,
    java_version: Optional[str] = None,
    mode: Optional[str] = None,
    cache_dir: Optional[Path] = None,
) -> bool:
    """
    Full pipeline:
      1. Build ModularKit  (with pre/post hooks, skipped if up-to-date)
      2. Build CoffeeLoader (skipped if up-to-date)
      3. Build Islands      (skipped if up-to-date)
      4. Assemble output directory
      5. Launch CoffeeLoader (blocking – Ctrl+C to stop)

    Pass fast_build=True to skip the Maven build steps (1-3) entirely.
    Pass clean=True to force rebuild of everything (ignores hash cache).
    """
    effective_mode = mode or cfg.BUILD_MODE
    effective_cache = cache_dir or (cfg.BUILD_DIR / ".build-cache")
    projects = cfg.get_projects()
    log.banner(
        "Build & Run Islands",
        f"{len(projects)} project(s)  →  launch  |  mode: {effective_mode}  |  force: {clean}",
    )

    env = _resolve_env(java_version)
    if env is None and cfg.JAVA_VERSION:
        return False

    if fast_build:
        log.info("--fast-build: skipping Maven build, using existing artifacts.")
    else:
        # --clean wipes hash cache so everything rebuilds
        if clean:
            hashermod.clear_cache(effective_cache)
            log.info("--clean: build cache cleared, all projects will rebuild.")

        # Build manifest map once for fingerprinting
        all_manifests: dict = {}
        for p in projects:
            m = hooksmod.ProjectManifest.load(Path(p["dir"]))
            if m is not None:
                all_manifests[m.artifact_id] = m

        total = len(projects)
        for i, project in enumerate(projects, 1):
            log.step(i, total, project["name"])

            manifest = hooksmod.ProjectManifest.load(Path(project["dir"]))
            artifact  = Path(project["artifact"]) if project.get("artifact") else None

            # ── hash-diff check ──────────────────────────────────────────
            if (
                not clean
                and manifest is not None
                and artifact is not None
                and hashermod.is_up_to_date(
                    Path(project["dir"]), manifest, all_manifests,
                    effective_mode, artifact, effective_cache,
                )
            ):
                log.info(f"[{project['name']}] ✓ up-to-date — skipping")
                continue

            # ── pre-build hooks ──────────────────────────────────────────
            ctx = hooksmod.build_hook_context(project, mode=effective_mode,
                                              verbose=verbose, workspace_dir=cfg.WORKSPACE)
            ok, pom_override, extra_mvn_args = hooksmod.run_hooks(
                "pre_build",
                [hooksmod.universal_prebuild],
                ctx,
            )
            if not ok:
                log.error(f"Pre-build hook failed for: {project['name']}")
                return False

            # ── maven build ──────────────────────────────────────────────
            ok = maven.build_project(
                project["name"],
                project["dir"],
                skip_tests=skip_tests,
                clean=clean,
                verbose=verbose,
                env=env,
                pom_override=pom_override,
                extra_maven_args=extra_mvn_args,
            )
            if not ok:
                log.error(f"Build pipeline aborted at: {project['name']}")
                return False

            # ── post-build hooks ─────────────────────────────────────────
            ok, _, _ = hooksmod.run_hooks("post_build", [], ctx)
            if not ok:
                log.error(f"Post-build hook failed for: {project['name']}")
                return False

            # ── update cache & cascade-invalidate dependents ─────────────
            if manifest is not None:
                hashermod.mark_built(
                    Path(project["dir"]), manifest, all_manifests,
                    effective_mode, effective_cache,
                )
                invalidated = hashermod.invalidate_dependents(
                    manifest.artifact_id, all_manifests, effective_cache
                )
                if invalidated:
                    log.info(f"  cache invalidated for: {', '.join(invalidated)}")

    if not _assemble_output(clean=clean_output):
        log.error("Failed to assemble output directory.")
        return False

    return _launch_coffeeloader(java_opts=java_opts, env=env)


def _find_launcher_jar() -> Optional[Path]:
    """
    Locate the first application-type project jar in the output directory.
    Falls back to any .jar in output/ (not in output/modules/).
    """
    if not cfg.OUTPUT_DIR.exists():
        return None
    # Prefer jars that match an application artifact name
    try:
        from hooks import ProjectManifest
        for project in cfg.get_projects():
            m = ProjectManifest.load(Path(project["dir"]))
            if m and m.project_type == "application":
                candidate = cfg.OUTPUT_DIR / Path(project["artifact"]).name
                if candidate.exists():
                    return candidate
    except Exception:
        pass
    # Fallback: any jar directly in output/ (not in modules/)
    for jar in cfg.OUTPUT_DIR.glob("*.jar"):
        return jar
    return None


def _launch_coffeeloader(
    *,
    java_opts: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> bool:
    jar = _find_launcher_jar()
    config_file = cfg.OUTPUT_DIR / "config.json"

    if jar is None or not jar.exists():
        log.error(
            f"No launcher jar found in {cfg.OUTPUT_DIR}. "
            "Run 'build-all' first or check that an application project is configured."
        )
        return False

    # Resolve the java binary: prefer the one from env's JAVA_HOME
    java_bin = "java"
    if env and "JAVA_HOME" in env:
        candidate = Path(env["JAVA_HOME"]) / "bin" / "java"
        if candidate.exists():
            java_bin = str(candidate)

    cmd = [java_bin]
    if java_opts:
        cmd += java_opts.split()
    cmd += [
        "-Dcoffeeloader.config=" + str(config_file),
        "-jar", str(jar),
    ]

    log.section("Launching application")
    log.info(f"Jar:         {jar.name}")
    log.info(f"Command:     {' '.join(cmd)}")
    log.info(f"Working dir: {cfg.OUTPUT_DIR}")
    log.info("Press Ctrl+C to stop.\n")

    proc = None
    try:
        proc = subprocess.Popen(cmd, cwd=cfg.OUTPUT_DIR, env=env)
        proc.wait()
    except KeyboardInterrupt:
        log.warn("Interrupt received – stopping…")
        if proc:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        log.info("Application stopped.")
    except FileNotFoundError:
        log.error(f"'{java_bin}' not found – please install a JDK and add it to PATH.")
        return False

    return True

