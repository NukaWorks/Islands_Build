"""
Central configuration for the Islands build automation system.
All paths are resolved relative to the workspace root.
"""
import os
from pathlib import Path

# ── Lazy hook imports (avoids circular deps at module load time) ──────────────
def _universal_hooks():
    from hooks import universal_prebuild
    return {"pre_build": [universal_prebuild], "post_build": []}

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

# ── Workspace layout ─────────────────────────────────────────────────────────
# The workspace root is the parent of the Build directory
BUILD_DIR   = Path(__file__).resolve().parent                  # …/Build
WORKSPACE   = BUILD_DIR.parent                                 # …/Islands (root)

MODULARKIT_DIR  = WORKSPACE / "ModularKit"
COFFEELOADER_DIR = WORKSPACE / "CoffeeLoader"
ISLANDS_DIR     = WORKSPACE / "Islands"

# ── Output / distribution ────────────────────────────────────────────────────
OUTPUT_DIR  = WORKSPACE / "output"
MODULES_DIR = OUTPUT_DIR / "modules"   # where Islands module JAR lands

# ── Artifact names produced by Maven (finalName in pom.xml) ─────────────────
MODULARKIT_ARTIFACT   = "ModularKit-1.8.3.jar"
COFFEELOADER_ARTIFACT = "CoffeeLoader-1.0.0-jar-with-dependencies.jar"
ISLANDS_ARTIFACT      = "islands-0.0.1-SNAPSHOT.jar"

COFFEELOADER_TARGET = COFFEELOADER_DIR / "target" / COFFEELOADER_ARTIFACT
ISLANDS_TARGET      = ISLANDS_DIR      / "target" / ISLANDS_ARTIFACT

# ── CoffeeLoader runtime config ──────────────────────────────────────────────
COFFEELOADER_OUTPUT_JAR = OUTPUT_DIR / COFFEELOADER_ARTIFACT
ISLANDS_MODULE_JAR      = MODULES_DIR / ISLANDS_ARTIFACT

COFFEELOADER_RUNTIME_CONFIG = {
    "port": 8080,
    "fileWatcher": True,
    "sources": [str(MODULES_DIR)],
}

# ── Git repository roots ─────────────────────────────────────────────────────
# All directories that are (or may be) independent git repos.
# Used by the `git` sub-commands in the CLI.
REPOS = [
    {"name": "Islands (root)", "dir": WORKSPACE},
    {"name": "ModularKit",     "dir": MODULARKIT_DIR},
    {"name": "CoffeeLoader",   "dir": COFFEELOADER_DIR},
    {"name": "Islands app",    "dir": ISLANDS_DIR},
]

# Projects in build/dependency order
PROJECTS = [
    {
        "name":     "ModularKit",
        "dir":      MODULARKIT_DIR,
        "artifact": MODULARKIT_DIR / "target" / MODULARKIT_ARTIFACT,
        "hooks":    _universal_hooks,
    },
    {
        "name":     "CoffeeLoader",
        "dir":      COFFEELOADER_DIR,
        "artifact": COFFEELOADER_TARGET,
        "hooks":    _universal_hooks,
    },
    {
        "name":     "Islands",
        "dir":      ISLANDS_DIR,
        "artifact": ISLANDS_TARGET,
        "hooks":    _universal_hooks,
    },
]

