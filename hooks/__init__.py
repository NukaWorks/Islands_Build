# Re-export the full public API from hooks.hooks so that
# `from hooks import X` and `import hooks as hooksmod` continue to work
# exactly as they did when hooks was a plain module (hooks.py).
from hooks.hooks import (
    HookContext,
    HookResult,
    Hook,
    ProjectManifest,
    PROJECT_TYPES,
    patch_pom,
    universal_prebuild,
    modularkit_prebuild,
    sync_module_json,
    remove_pom_dependency,
    sync_pom_versions,
    run_hooks,
    build_hook_context,
)

__all__ = [
    "HookContext",
    "HookResult",
    "Hook",
    "ProjectManifest",
    "PROJECT_TYPES",
    "patch_pom",
    "universal_prebuild",
    "modularkit_prebuild",
    "sync_module_json",
    "remove_pom_dependency",
    "sync_pom_versions",
    "run_hooks",
    "build_hook_context",
]

