"""
Maven build helpers.
"""
import os
import shutil
import subprocess
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
    pom_override: Optional[Path] = None,
) -> bool:
    """
    Run 'mvn <goals>' inside *project_dir*, streaming all output live.
    Pass *env* to override environment variables (e.g. JAVA_HOME).
    Pass *pom_override* to use a different pom file (e.g. .buildconfig-pom.xml).

    Returns True on success, False on failure.
    """
    cmd = ["mvn"] + goals
    if pom_override is not None:
        cmd += ["-f", str(pom_override)]
    if skip_tests:
        cmd += ["-DskipTests"]
    if extra_args:
        cmd += extra_args
    if not verbose:
        # batch-mode removes download progress spam; output still streams live
        cmd += ["--batch-mode"]

    # Resolve mvn from the provided env's PATH so the right JDK is used
    effective_env = env if env is not None else os.environ.copy()
    mvn_bin = shutil.which("mvn", path=effective_env.get("PATH", os.environ.get("PATH", "")))
    if mvn_bin:
        cmd[0] = mvn_bin

    java_home = (env or {}).get("JAVA_HOME", "")
    java_tag = f"  [JAVA_HOME={java_home}]" if java_home else ""
    log.info(f"Running: {' '.join(cmd)}  (in {project_dir.name}){java_tag}")
    start = time.time()

    try:
        # stdout/stderr are NOT captured — they go straight to the terminal
        # env=None means inherit the current process env (ambient PATH/JAVA_HOME)
        result = subprocess.run(cmd, cwd=project_dir, env=env if env is not None else os.environ.copy())
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
    clean: bool = False,
    verbose: bool = False,
    env: Optional[Dict[str, str]] = None,
    pom_override: Optional[Path] = None,
    extra_maven_args: Optional[List[str]] = None,
) -> bool:
    """Build a single Maven project and report the result."""
    log.section(f"Building  {name}")
    if goals is not None:
        effective_goals: List[str] = goals
    elif clean:
        effective_goals = ["clean", "install"]
    else:
        effective_goals = ["install"]
    all_extra = list(extra_maven_args or [])
    ok = run_maven(
        project_dir,
        effective_goals,
        skip_tests=skip_tests,
        verbose=verbose,
        env=env,
        pom_override=pom_override,
        extra_args=all_extra if all_extra else None,
    )
    if ok:
        log.success(f"{name} — build OK")
    else:
        log.error(f"{name} — build FAILED")
    return ok

