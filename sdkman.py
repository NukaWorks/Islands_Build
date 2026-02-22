"""
SDKMan integration helpers.

Allows the build system to:
  - Locate the sdkman installation
  - List installed Java candidates
  - Resolve JAVA_HOME for a specific candidate identifier
  - Install a Java candidate via sdkman
  - Build an env dict (JAVA_HOME + PATH) suitable for subprocess calls
"""
import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import logger as log

# ── sdkman paths ──────────────────────────────────────────────────────────────
SDKMAN_DIR = Path(os.environ.get("SDKMAN_DIR", Path.home() / ".sdkman"))
SDKMAN_INIT = SDKMAN_DIR / "bin" / "sdkman-init.sh"
JAVA_CANDIDATES_DIR = SDKMAN_DIR / "candidates" / "java"


def is_available() -> bool:
    """Return True if sdkman is installed on this machine."""
    return SDKMAN_INIT.exists()


def installed_candidates() -> List[Tuple[str, Path]]:
    """
    Return a list of (identifier, java_home) for every locally-installed
    Java candidate, excluding the 'current' symlink.
    """
    if not JAVA_CANDIDATES_DIR.exists():
        return []
    result = []
    for entry in sorted(JAVA_CANDIDATES_DIR.iterdir()):
        if entry.name == "current":
            continue
        if entry.is_dir() or entry.is_symlink():
            result.append((entry.name, entry.resolve()))
    return result


def current_candidate() -> Optional[Tuple[str, Path]]:
    """Return (identifier, java_home) for the currently active candidate, or None."""
    current = JAVA_CANDIDATES_DIR / "current"
    if not current.exists():
        return None
    resolved = current.resolve()
    # derive identifier from the symlink target folder name
    identifier = resolved.name
    return identifier, resolved


def resolve_java_home(identifier: str) -> Optional[Path]:
    """
    Given a candidate identifier (e.g. '24.0.2-tem' or '21.0.6-tem'),
    return its JAVA_HOME path, or None if not installed.

    Also accepts a plain major version string like '21' or '24'
    and will find the first installed match.
    """
    candidates = installed_candidates()

    # Exact match first
    for name, home in candidates:
        if name == identifier:
            return home

    # Prefix/major-version match (e.g. '24' matches '24.0.2-tem')
    for name, home in candidates:
        if name.startswith(identifier):
            return home

    return None


def build_env(java_home: Path) -> Dict[str, str]:
    """
    Return a copy of os.environ with JAVA_HOME set and the JDK bin
    prepended to PATH so Maven picks up the right java.
    """
    env = os.environ.copy()
    env["JAVA_HOME"] = str(java_home)
    env["PATH"] = str(java_home / "bin") + os.pathsep + env.get("PATH", "")
    return env


def _run_sdk_cmd(args: List[str]) -> bool:
    """Run a sdkman command by sourcing sdkman-init.sh in a bash subshell."""
    if not is_available():
        log.error("sdkman is not installed (expected at ~/.sdkman).")
        return False

    cmd_str = (
        f'source "{SDKMAN_INIT}" && sdk {" ".join(args)}'
    )
    log.info(f"sdkman: sdk {' '.join(args)}")
    result = subprocess.run(
        ["bash", "-c", cmd_str],
        env={**os.environ, "SDKMAN_AUTO_ANSWER": "true"},
    )
    return result.returncode == 0


def install(identifier: str) -> bool:
    """Install a Java candidate via sdkman (e.g. '24.0.2-tem')."""
    if resolve_java_home(identifier) is not None:
        log.info(f"Java {identifier} is already installed.")
        return True
    log.info(f"Installing Java {identifier} via sdkman…")
    ok = _run_sdk_cmd(["install", "java", identifier])
    if ok:
        log.success(f"Java {identifier} installed.")
    else:
        log.error(f"Failed to install Java {identifier}.")
    return ok


def ensure_java(identifier: str, *, auto_install: bool = True) -> Optional[Path]:
    """
    Resolve JAVA_HOME for *identifier*.
    If not installed and *auto_install* is True, install it first.
    Returns the JAVA_HOME Path or None on failure.
    """
    home = resolve_java_home(identifier)
    if home:
        log.info(f"Using Java {identifier}  →  {home}")
        return home

    if not auto_install:
        log.error(
            f"Java {identifier} is not installed. "
            "Run:  python build.py sdk install <identifier>"
        )
        return None

    if not is_available():
        log.error(
            "sdkman is not available and the requested Java version is not "
            "installed. Please install Java manually or install sdkman."
        )
        return None

    log.warn(f"Java {identifier} not found locally – installing via sdkman…")
    if not install(identifier):
        return None
    return resolve_java_home(identifier)


def print_candidates() -> None:
    """Pretty-print all locally installed Java candidates."""
    current = current_candidate()
    current_name = current[0] if current else None
    candidates = installed_candidates()

    if not candidates:
        log.warn("No Java candidates installed via sdkman.")
        return

    try:
        from rich.table import Table
        from rich.console import Console
        table = Table(title="Installed Java Candidates (sdkman)", show_lines=False)
        table.add_column("Identifier",  style="cyan",  no_wrap=True)
        table.add_column("Status",      justify="center")
        table.add_column("JAVA_HOME",   style="dim", overflow="fold")
        for name, home in candidates:
            status = "[bold green]current[/bold green]" if name == current_name else ""
            table.add_row(name, status, str(home))
        Console().print(table)
    except ImportError:
        print(f"\n{'Identifier':<22}  {'Status':<10}  JAVA_HOME")
        print("─" * 80)
        for name, home in candidates:
            status = "(current)" if name == current_name else ""
            print(f"{name:<22}  {status:<10}  {home}")
        print()

