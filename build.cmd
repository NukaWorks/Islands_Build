@echo off
REM ─────────────────────────────────────────────────────────────────────────
REM  Islands Build Launcher  (Windows CMD / PowerShell)
REM  Usage:  build.cmd <command> [options...]
REM
REM  Examples:
REM    build.cmd build-all
REM    build.cmd run-islands --java-version 24.0.2-tem
REM    build.cmd idea --force
REM    build.cmd sdk list
REM    build.cmd --help
REM ─────────────────────────────────────────────────────────────────────────
setlocal EnableDelayedExpansion

REM Resolve the directory this script lives in
set "SCRIPT_DIR=%~dp0"
set "BUILD_SCRIPT=%SCRIPT_DIR%\build.py"

REM ── Locate Python 3 ──────────────────────────────────────────────────────
set "PYTHON="
where python >nul 2>&1 && (
    for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do (
        set "VER=%%v"
        if "!VER:~0,1!"=="3" set "PYTHON=python"
    )
)
if not defined PYTHON (
    where python3 >nul 2>&1 && set "PYTHON=python3"
)
if not defined PYTHON (
    echo Error: Python 3 is required but was not found on PATH. 1>&2
    exit /b 1
)

REM ── Optional: activate a JAVA_HOME via SDKMAN_DIR (WSL/Cygwin style) ─────
REM  On native Windows you would set JAVA_HOME and PATH before calling this
REM  script, e.g. from an SDKMAN4W or Coursier managed installation.

%PYTHON% "%BUILD_SCRIPT%" %*

