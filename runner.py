"""
High-level run configurations:
  - build_all_and_test  : build every project, run tests for each
  - build_and_run_islands: build all, assemble output dir, launch CoffeeLoader
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Optional

import config as cfg
import fs
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
    log.banner(
        "Build All & Test",
        "ModularKit → CoffeeLoader → Islands  (tests enabled)",
    )

    env = _resolve_env(java_version)
    if env is None and cfg.JAVA_VERSION:
        return False   # resolution failed

    total = len(cfg.PROJECTS)
    for i, project in enumerate(cfg.PROJECTS, 1):
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
    1. Create output/ and output/modules/
    2. Copy CoffeeLoader fat-jar → output/
    3. Copy Islands jar          → output/modules/
    4. Write CoffeeLoader config → output/config.json
    """
    log.section("Assembling output directory")

    if clean:
        fs.clean_output(cfg.OUTPUT_DIR)
    else:
        fs.ensure_dir(cfg.OUTPUT_DIR)
        fs.ensure_dir(cfg.MODULES_DIR)

    ok = fs.copy_artifact(cfg.COFFEELOADER_TARGET, cfg.COFFEELOADER_OUTPUT_JAR)
    if not ok:
        return False

    ok = fs.copy_artifact(cfg.ISLANDS_TARGET, cfg.ISLANDS_MODULE_JAR)
    if not ok:
        return False

    runtime_cfg = {
        "port": cfg.COFFEELOADER_RUNTIME_CONFIG["port"],
        "fileWatcher": cfg.COFFEELOADER_RUNTIME_CONFIG["fileWatcher"],
        "sources": [str(cfg.MODULES_DIR)],
    }
    fs.write_json(cfg.OUTPUT_DIR / "config.json", runtime_cfg)
    return True


def build_and_run_islands(
    *,
    skip_tests: bool = True,
    clean_output: bool = True,
    verbose: bool = False,
    java_opts: Optional[str] = None,
    java_version: Optional[str] = None,
) -> bool:
    """
    Full pipeline:
      1. Build ModularKit
      2. Build CoffeeLoader
      3. Build Islands
      4. Assemble output directory
      5. Launch CoffeeLoader (blocking – Ctrl+C to stop)
    """
    log.banner(
        "Build & Run Islands",
        "ModularKit → CoffeeLoader → Islands  then launch",
    )

    env = _resolve_env(java_version)
    if env is None and cfg.JAVA_VERSION:
        return False

    total = len(cfg.PROJECTS)
    for i, project in enumerate(cfg.PROJECTS, 1):
        log.step(i, total, project["name"])
        ok = maven.build_project(
            project["name"],
            project["dir"],
            skip_tests=skip_tests,
            verbose=verbose,
            env=env,
        )
        if not ok:
            log.error(f"Build pipeline aborted at: {project['name']}")
            return False

    if not _assemble_output(clean=clean_output):
        log.error("Failed to assemble output directory.")
        return False

    return _launch_coffeeloader(java_opts=java_opts, env=env)


def _launch_coffeeloader(
    *,
    java_opts: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> bool:
    jar = cfg.COFFEELOADER_OUTPUT_JAR
    config_file = cfg.OUTPUT_DIR / "config.json"

    if not jar.exists():
        log.error(f"CoffeeLoader jar not found: {jar}")
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

    log.section("Launching CoffeeLoader")
    log.info(f"Command: {' '.join(cmd)}")
    log.info(f"Working dir: {cfg.OUTPUT_DIR}")
    log.info("Press Ctrl+C to stop.\n")

    proc = None
    try:
        proc = subprocess.Popen(cmd, cwd=cfg.OUTPUT_DIR, env=env)
        proc.wait()
    except KeyboardInterrupt:
        log.warn("Interrupt received – stopping CoffeeLoader…")
        if proc:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        log.info("CoffeeLoader stopped.")
    except FileNotFoundError:
        log.error(f"'{java_bin}' not found – please install a JDK and add it to PATH.")
        return False

    return True

