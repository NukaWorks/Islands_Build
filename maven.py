"""
Maven build helpers.
"""
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import logger as log


def run_maven(
    project_dir: Path,
    goals: List[str],
    *,
    skip_tests: bool = False,
    extra_args: Optional[List[str]] = None,
    verbose: bool = False,
    env: Optional[Dict[str, str]] = None,
) -> bool:
    """
    Run 'mvn <goals>' inside *project_dir*, streaming all output live.
    Pass *env* to override environment variables (e.g. JAVA_HOME).

    Returns True on success, False on failure.
    """
    cmd = ["mvn"] + goals
    if skip_tests:
        cmd += ["-DskipTests"]
    if extra_args:
        cmd += extra_args
    if not verbose:
        # batch-mode removes download progress spam; output still streams live
        cmd += ["--batch-mode"]

    java_home = (env or {}).get("JAVA_HOME", "")
    java_tag = f"  [JAVA_HOME={java_home}]" if java_home else ""
    log.info(f"Running: {' '.join(cmd)}  (in {project_dir.name}){java_tag}")
    start = time.time()

    try:
        # stdout/stderr are NOT captured — they go straight to the terminal
        result = subprocess.run(cmd, cwd=project_dir, env=env)
    except FileNotFoundError:
        log.error("'mvn' not found – please install Apache Maven and add it to PATH.")
        return False

    elapsed = time.time() - start

    if result.returncode != 0:
        log.error(
            f"Maven failed after {log.duration(elapsed)} "
            f"(exit {result.returncode})"
        )
        return False

    log.success(f"Maven succeeded in {log.duration(elapsed)}")
    return True


def build_project(
    name: str,
    project_dir: Path,
    *,
    goals: Optional[List[str]] = None,
    skip_tests: bool = False,
    verbose: bool = False,
    env: Optional[Dict[str, str]] = None,
) -> bool:
    """Build a single Maven project and report the result."""
    log.section(f"Building  {name}")
    goals = goals or ["clean", "install"]
    ok = run_maven(project_dir, goals, skip_tests=skip_tests, verbose=verbose, env=env)
    if ok:
        log.success(f"{name} — build OK")
    else:
        log.error(f"{name} — build FAILED")
    return ok

