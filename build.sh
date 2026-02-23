#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  Islands Build  —  bash / zsh  launcher & alias installer
#
#  ① Executable  (direct run):
#       ./build.sh <command> [options...]
#       ./build.sh build-all --java-version 24.0.2-tem
#
#  ② Sourceable  (register aliases in the current shell):
#       source ./build.sh          # bash
#
#     After sourcing, the following aliases are available:
#       islands          → python3 …/Build/build.py
#       islands-build    → islands build-all
#       islands-run      → islands run-islands
#       islands-clean    → islands clean
#       islands-status   → islands statussys
#       islands-info     → islands info
#       islands-idea     → islands idea
#       islands-sdk      → islands sdk
# ─────────────────────────────────────────────────────────────────────────────

# ── Detect whether we are being sourced or executed ──────────────────────────
_islands_is_sourced() {
    # ZSH
    if [[ -n "${ZSH_VERSION-}" ]]; then
        [[ "${ZSH_EVAL_CONTEXT-}" == *:file:* || "${ZSH_EVAL_CONTEXT-}" == *file* ]] && return 0
        # fallback: $0 is the script name only when executed, not when sourced
        [[ "$0" != "${(%):-%x}" ]] && return 0
        return 1
    fi
    # Bash
    [[ "${BASH_SOURCE[0]}" != "$0" ]]
}

# ── Resolve the directory that contains THIS script ──────────────────────────
_islands_script_dir() {
    if [[ -n "${ZSH_VERSION-}" ]]; then
        # ZSH: ${(%):-%x} expands to the current file even when sourced
        echo "$(cd "$(dirname "${(%):-%x}")" && pwd)"
    else
        # Bash: BASH_SOURCE[0] is the script path in both modes
        echo "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    fi
}

# ── Source sdkman safely (works for both bash and zsh) ───────────────────────
_islands_load_sdkman() {
    local init="${SDKMAN_DIR:-$HOME/.sdkman}/bin/sdkman-init.sh"
    if [[ -f "$init" ]]; then
        # sdkman-init.sh references ZSH_VERSION / BASH_VERSION; guard unbound vars
        # shellcheck disable=SC1090
        set +u 2>/dev/null || true
        # suppress "already initialised" noise when sourced multiple times
        source "$init" 2>/dev/null || true
        set -u 2>/dev/null || true
    fi
}

# ── Locate python3 ───────────────────────────────────────────────────────────
_islands_find_python() {
    if command -v python3 &>/dev/null; then
        echo python3
    elif command -v python &>/dev/null && python --version 2>&1 | grep -q "^Python 3"; then
        echo python
    else
        return 1
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
#  MODE A – sourced: register aliases in the calling shell
# ─────────────────────────────────────────────────────────────────────────────
_islands_register_aliases() {
    local script_dir
    script_dir="$(_islands_script_dir)"
    local build_script="$script_dir/build.py"

    local python
    if ! python="$(_islands_find_python)"; then
        echo "islands: Error: Python 3 not found on PATH." >&2
        return 1
    fi

    _islands_load_sdkman

    # Core function (used by all aliases; survives alias expansion)
    # shellcheck disable=SC2139
    eval "islands() { \"$python\" \"$build_script\" \"\$@\"; }"

    alias islands-build='islands build-all'
    alias islands-run='islands run-islands'
    alias islands-clean='islands clean'
    alias islands-status='islands status'
    alias islands-info='islands info'
    alias islands-idea='islands idea'
    alias islands-sdk='islands sdk'
    # git aliases
    alias islands-git='islands git'
    alias islands-git-status='islands git status'
    alias islands-git-branches='islands git branches'
    alias islands-git-fetch='islands git fetch'
    alias islands-git-pull='islands git pull'
    # repo (Google repo tool) aliases
    alias islands-repo='islands repo'
    alias islands-repo-status='islands repo status'
    alias islands-repo-info='islands repo info'
    alias islands-repo-sync='islands repo sync'
    alias islands-repo-manifest='islands repo manifest'
    # hooks aliases
    alias islands-hooks='islands hooks'
    alias islands-hooks-list='islands hooks list'

    echo "islands: aliases registered. Try: islands --help"
    echo "  islands                  run any build command"
    echo "  islands-build            build-all"
    echo "  islands-run              run-islands"
    echo "  islands-clean            clean"
    echo "  islands-status           status"
    echo "  islands-info             info"
    echo "  islands-idea             idea"
    echo "  islands-sdk              sdk <list|install|use>"
    echo "  islands-git-status       git status  (all repos)"
    echo "  islands-git-branches     git branches (all repos)"
    echo "  islands-git-fetch        git fetch   (all repos)"
    echo "  islands-git-pull         git pull    (all repos)"
    echo "  islands git checkout <branch>        switch branch in all repos"
    echo "  islands-repo-status      repo status"
    echo "  islands-repo-info        repo info"
    echo "  islands-repo-sync        repo sync"
    echo "  islands-repo-manifest    repo manifest (show/edit default.xml)"
    echo "  islands repo checkout <branch>       switch branch via repo"
    echo "  islands-hooks-list       list all registered hooks"
    echo "  islands hooks run <project> [--mode local|devel|release]"
}

# ─────────────────────────────────────────────────────────────────────────────
#  MODE B – executed directly: run the build script
# ─────────────────────────────────────────────────────────────────────────────
_islands_exec() {
    set -euo pipefail

    local script_dir
    script_dir="$(_islands_script_dir)"
    local build_script="$script_dir/Build/build.py"

    _islands_load_sdkman

    local python
    if ! python="$(_islands_find_python)"; then
        echo "Error: Python 3 is required but was not found on PATH." >&2
        exit 1
    fi

    exec "$python" "$build_script" "$@"
}

# ─────────────────────────────────────────────────────────────────────────────
#  Dispatch
# ─────────────────────────────────────────────────────────────────────────────
if _islands_is_sourced; then
    _islands_register_aliases
else
    _islands_exec "$@"
fi

