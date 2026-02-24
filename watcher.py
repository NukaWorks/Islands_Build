"""
File-system watcher for the Islands build automation.

Watch mode  (``islands run --watch``)
--------------------------------------
The watcher runs alongside a live CoffeeLoader process.  It polls every
project's source tree for changes, rebuilds only what changed (using the
existing hash-diff system), then decides how to deliver the update:

  Hot-swap  – re-assemble output/ and let CoffeeLoader's built-in
              ``fileWatcher`` reload the jar without any JVM restart.
              Used when all changed projects are ModularKit *modules*
              (have a ``module`` block in project.json) AND the
              CoffeeLoader ``config.json`` has ``fileWatcher: true``.

  Relaunch  – stop the running CoffeeLoader process, re-assemble
              output/, then start a fresh process.
              Used when:
                • The changed project is the launcher application
                  (application type, no module block), OR
                • A library (no module block) changed — the JVM classpath
                  cannot be updated at runtime, OR
                • A module changed but CoffeeLoader fileWatcher is OFF.

Architecture
------------
  MainThread  – watcher poll loop (Ctrl+C stops everything)
  Thread-1    – CoffeeLoader subprocess (_AppProcess, restartable)

Debounce
--------
After a change is detected a short ``debounce`` wait is applied before
rebuilding so that a burst of editor saves only triggers one rebuild cycle.

Public API
----------
  watch_and_run(...)  – entry point called from runner / build.py
"""
from __future__ import annotations

import json
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

import config as cfg
import hasher as hashermod
import hooks as hooksmod
import logger as log
import maven
import runner as runnermod


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _coffeeloader_filewatcher_enabled() -> bool:
    """
    Read ``output/config.json`` and return the value of ``fileWatcher``.
    Defaults to False if the file is missing or the key is absent.
    """
    config_file = cfg.OUTPUT_DIR / "config.json"
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
        return bool(data.get("fileWatcher", False))
    except Exception:
        return False


def _classify_changed(
    changed_aids: List[str],
    all_manifests: dict,
) -> tuple[bool, bool]:
    """
    Inspect the set of changed artifact IDs and return:

      (needs_relaunch, has_hot_swappable)

    needs_relaunch   – True if ANY changed project requires a full process
                       restart (launcher app, plain library without module
                       block, or module with fileWatcher off).
    has_hot_swappable – True if at least one changed project is a module
                        that can be hot-swapped.

    The caller should relaunch if ``needs_relaunch`` is True regardless of
    ``has_hot_swappable``.
    """
    fw_enabled = _coffeeloader_filewatcher_enabled()
    needs_relaunch    = False
    has_hot_swappable = False

    for aid in changed_aids:
        m = all_manifests.get(aid)
        if m is None:
            needs_relaunch = True
            continue

        is_module  = bool(m.module)          # has a ModularKit module descriptor
        is_app     = m.is_application()
        is_library = m.is_library()

        if is_app and not is_module:
            # The launcher itself changed — must restart
            log.info(f"  [{m.name}] launcher application changed → relaunch required")
            needs_relaunch = True

        elif is_library and not is_module:
            # Plain classpath library (e.g. ModularKit) — JVM classpath can't hot-swap
            log.info(f"  [{m.name}] classpath library changed → relaunch required")
            needs_relaunch = True

        elif is_module:
            # ModularKit module jar (has module block)
            if fw_enabled:
                log.info(f"  [{m.name}] module changed, fileWatcher ON → hot-swap")
                has_hot_swappable = True
            else:
                log.info(f"  [{m.name}] module changed, fileWatcher OFF → relaunch required")
                needs_relaunch = True

        else:
            # Unknown / unclassifiable — safe fallback
            needs_relaunch = True

    return needs_relaunch, has_hot_swappable


def _aid(project: dict, all_manifests: dict) -> str:
    """Return the artifactId for a project dict, or '' if no manifest."""
    m = hooksmod.ProjectManifest.load(Path(project["dir"]))
    return m.artifact_id if m else ""


# ─────────────────────────────────────────────────────────────────────────────
# Rebuild helper
# ─────────────────────────────────────────────────────────────────────────────

def _rebuild_projects(
    artifact_ids: List[str],
    projects: list,
    all_manifests: dict,
    *,
    mode: str,
    skip_tests: bool,
    verbose: bool,
    env: Optional[Dict],
    cache_dir: Path,
) -> bool:
    """
    Rebuild every project whose artifact_id is in *artifact_ids*, in
    topological order.  Updates the cache and cascade-invalidates dependents
    after each successful build, adding them to the rebuild queue.

    Returns True if all triggered builds succeeded.
    """
    rebuild_set = set(artifact_ids)
    to_build = [p for p in projects if _aid(p, all_manifests) in rebuild_set]

    for project in to_build:
        manifest = hooksmod.ProjectManifest.load(Path(project["dir"]))
        if manifest is None:
            continue

        log.section(f"[watch] Rebuilding  {project['name']}")

        ctx = hooksmod.build_hook_context(
            project, mode=mode, verbose=verbose, workspace_dir=cfg.WORKSPACE
        )
        ok, pom_override, extra_mvn_args = hooksmod.run_hooks(
            "pre_build", [hooksmod.universal_prebuild], ctx
        )
        if not ok:
            log.error(f"Pre-build hook failed for: {project['name']}")
            return False

        ok = maven.build_project(
            project["name"],
            project["dir"],
            skip_tests=skip_tests,
            clean=False,
            verbose=verbose,
            env=env,
            pom_override=pom_override,
            extra_maven_args=extra_mvn_args,
        )
        if not ok:
            log.error(f"[watch] Build failed: {project['name']} — continuing to watch…")
            return False

        hooksmod.run_hooks("post_build", [], ctx)

        hashermod.mark_built(
            Path(project["dir"]), manifest, all_manifests, mode, cache_dir
        )
        newly_stale = hashermod.invalidate_dependents(
            manifest.artifact_id, all_manifests, cache_dir
        )
        if newly_stale:
            log.info(f"  cascade: adding to rebuild queue — {', '.join(newly_stale)}")
            rebuild_set.update(newly_stale)
            extra = [
                p for p in projects
                if _aid(p, all_manifests) in newly_stale and p not in to_build
            ]
            to_build.extend(extra)

    return True


# ─────────────────────────────────────────────────────────────────────────────
# CoffeeLoader REST API bridge
# ─────────────────────────────────────────────────────────────────────────────

def _read_coffeeloader_api_config() -> dict:
    """
    Read ``output/config.json`` and return a dict with keys:
      port     – int, default 8080
      apiKeys  – list[str], first non-empty entry used for auth
      jwtSecret – str (not used directly; token obtained via /api/auth/token)
    """
    config_file = cfg.OUTPUT_DIR / "config.json"
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
        return {
            "port": int(data.get("port", 8080)),
            "apiKeys": data.get("apiKeys", []),
        }
    except Exception:
        return {"port": 8080, "apiKeys": []}


class _WatcherBridge:
    """
    Thin HTTP client that calls CoffeeLoader's /api/watcher/* endpoints
    before and after a JAR replacement to prevent ModularKit from reading
    a partially-written file.

    Usage::
        bridge = _WatcherBridge()
        bridge.prepare_rebuild(module_uuids=[...], source_uuids=[...])
        # … replace JARs …
        bridge.rebuild_complete(source_uuids=[...])

    All calls are best-effort: if CoffeeLoader is not running (e.g. during
    a full relaunch) the bridge silently skips the call.
    """

    _TOKEN_TTL = 3500  # refresh token after ~58 min (token valid for 24 h)

    def __init__(self) -> None:
        self._base_url: str = ""
        self._api_key:  str = ""
        self._token:    str = ""
        self._token_ts: float = 0.0

    # ── configuration ─────────────────────────────────────────────────────

    def configure(self) -> None:
        """Re-read output/config.json to pick up port / apiKey."""
        api_cfg = _read_coffeeloader_api_config()
        self._base_url = f"http://localhost:{api_cfg['port']}"
        keys = [k for k in api_cfg.get("apiKeys", []) if k]
        self._api_key = keys[0] if keys else ""
        self._token   = ""       # force re-auth on next call
        self._token_ts = 0.0

    # ── auth ──────────────────────────────────────────────────────────────

    def _ensure_token(self) -> bool:
        """Obtain / refresh a JWT token. Returns False if no API key is set."""
        if not self._api_key:
            return False
        if self._token and (time.time() - self._token_ts) < self._TOKEN_TTL:
            return True
        try:
            body = json.dumps({"apiKey": self._api_key}).encode()
            req  = urllib.request.Request(
                f"{self._base_url}/api/auth/token",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                self._token    = data.get("token", "")
                self._token_ts = time.time()
                return bool(self._token)
        except Exception as exc:
            log.warn(f"[bridge] Auth failed: {exc}")
            return False

    # ── low-level POST ─────────────────────────────────────────────────────

    def _post(self, path: str, payload: dict) -> Optional[dict]:
        if not self._base_url:
            self.configure()
        if not self._ensure_token():
            return None
        try:
            body = json.dumps(payload).encode()
            req  = urllib.request.Request(
                f"{self._base_url}{path}",
                data=body,
                headers={
                    "Content-Type":  "application/json",
                    "Authorization": f"Bearer {self._token}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.URLError:
            # CoffeeLoader not reachable — not an error during relaunch
            return None
        except Exception as exc:
            log.warn(f"[bridge] POST {path} failed: {exc}")
            return None

    # ── public API ────────────────────────────────────────────────────────

    def prepare_rebuild(
        self,
        *,
        module_uuids: Optional[List[str]] = None,
        source_uuids: Optional[List[str]] = None,
    ) -> bool:
        """
        Stop + unload the listed modules/sources so CoffeeLoader won't read
        the JAR files while they are being replaced on disk.

        Returns True when CoffeeLoader confirmed the quiesce; False if the
        server was unreachable (safe to proceed — full relaunch will follow).
        """
        payload: dict = {
            "moduleUuids": module_uuids or [],
            "sourceUuids": source_uuids or [],
        }
        result = self._post("/api/watcher/prepare-rebuild", payload)
        if result is None:
            return False
        errors = result.get("errors", [])
        if errors:
            log.warn(f"[bridge] prepare-rebuild errors: {errors}")
        stopped  = result.get("stopped",  [])
        unloaded = result.get("unloaded", [])
        if stopped or unloaded:
            log.info(f"[bridge] stopped={stopped}  unloaded={unloaded}")
        return True

    def rebuild_complete(self, *, source_uuids: Optional[List[str]] = None) -> bool:
        """
        Signal that new JARs are in place so CoffeeLoader can restart the
        modules that were quiesced by prepare_rebuild.

        Returns True on success; False if the server was unreachable.
        """
        payload: dict = {"sourceUuids": source_uuids or []}
        result = self._post("/api/watcher/rebuild-complete", payload)
        if result is None:
            return False
        errors    = result.get("errors",    [])
        restarted = result.get("restarted", [])
        if errors:
            log.warn(f"[bridge] rebuild-complete errors: {errors}")
        if restarted:
            log.info(f"[bridge] restarted={restarted}")
        return True


# ─────────────────────────────────────────────────────────────────────────────
# Restartable CoffeeLoader process wrapper
# ─────────────────────────────────────────────────────────────────────────────

class _AppProcess:
    """
    Manages the CoffeeLoader subprocess.

    Unlike a simple thread wrapper this class is *restartable*: calling
    ``restart()`` terminates the current process and launches a new one,
    letting the watch loop relaunch without replacing the thread object.

    The subprocess runs inside a dedicated daemon thread so it does not
    block the main watch loop.  A new daemon thread is spawned on each
    ``start()`` / ``restart()`` call.
    """

    def __init__(self, *, java_opts: Optional[str], env: Optional[Dict]) -> None:
        self.java_opts = java_opts
        self.env       = env
        self._proc: Optional[subprocess.Popen]  = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._cmd: List[str] = []

    # ── internal ──────────────────────────────────────────────────────────

    def _build_cmd(self) -> Optional[List[str]]:
        jar = runnermod._find_launcher_jar()
        config_file = cfg.OUTPUT_DIR / "config.json"
        if jar is None or not jar.exists():
            log.error("[watch] No launcher jar found — cannot start application.")
            return None

        java_bin = "java"
        if self.env and "JAVA_HOME" in self.env:
            candidate = Path(self.env["JAVA_HOME"]) / "bin" / "java"
            if candidate.exists():
                java_bin = str(candidate)

        cmd = [java_bin]
        if self.java_opts:
            cmd += self.java_opts.split()
        cmd += ["-Dcoffeeloader.config=" + str(config_file), "-jar", str(jar)]
        return cmd

    def _run(self, cmd: List[str]) -> None:
        try:
            with self._lock:
                self._proc = subprocess.Popen(cmd, cwd=cfg.OUTPUT_DIR, env=self.env)
            self._proc.wait()
        except FileNotFoundError:
            log.error(f"[watch] java not found: {cmd[0]}")

    # ── public ────────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Launch CoffeeLoader for the first time."""
        cmd = self._build_cmd()
        if cmd is None:
            return False
        self._cmd = cmd
        log.section("Launching application  [watch mode]")
        log.info(f"Jar:     {Path(cmd[-1]).name}")
        log.info(f"Command: {' '.join(cmd)}")
        log.info("Hot-swap active — save a source file to trigger a rebuild.\n")
        self._thread = threading.Thread(target=self._run, args=(cmd,), daemon=True,
                                        name="coffeeloader")
        self._thread.start()
        return True

    def restart(self) -> bool:
        """Stop the current process and launch a fresh one."""
        log.info("[watch] Relaunching application…")
        self._stop_proc()
        if self._thread:
            self._thread.join(timeout=8)

        cmd = self._build_cmd()
        if cmd is None:
            return False
        self._cmd = cmd
        log.section("Relaunching application  [watch mode]")
        log.info(f"Jar:     {Path(cmd[-1]).name}")
        self._thread = threading.Thread(target=self._run, args=(cmd,), daemon=True,
                                        name="coffeeloader")
        self._thread.start()
        return True

    def stop(self) -> None:
        """Terminate gracefully, then wait for the thread."""
        self._stop_proc()
        if self._thread:
            self._thread.join(timeout=8)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _stop_proc(self) -> None:
        with self._lock:
            proc = self._proc
        if proc and proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def watch_and_run(
    *,
    skip_tests: bool = True,
    clean: bool = False,
    verbose: bool = False,
    java_opts: Optional[str] = None,
    java_version: Optional[str] = None,
    mode: Optional[str] = None,
    cache_dir: Optional[Path] = None,
    poll_interval: float = 2.0,
    debounce: float = 1.0,
) -> bool:
    """
    Full watch-mode pipeline:

      1. Initial build of all projects (respects hash cache unless ``clean``).
      2. Initial assemble of output/ directory.
      3. Launch CoffeeLoader via _AppProcess (background daemon thread).
      4. Poll loop:
           a. Compute fingerprints for all projects.
           b. On change: rebuild affected + dependents (with cascade).
           c. Classify changes:
                - Module jars + fileWatcher ON  → hot-swap (re-assemble only)
                - Launcher app / plain library  → stop + relaunch
                - Module jars + fileWatcher OFF → stop + relaunch
      5. Ctrl+C → stop CoffeeLoader, exit cleanly.
    """
    effective_mode  = mode or cfg.BUILD_MODE
    effective_cache = cache_dir or (cfg.BUILD_DIR / ".build-cache")

    env = runnermod._resolve_env(java_version)
    if env is None and cfg.JAVA_VERSION:
        return False

    projects = cfg.get_projects()

    # ── Step 1: initial build ──────────────────────────────────────────────
    log.banner(
        "Build & Run  [watch mode]",
        f"{len(projects)} project(s)  |  mode: {effective_mode}  |  "
        f"poll: {poll_interval}s  |  force: {clean}",
    )

    if clean:
        hashermod.clear_cache(effective_cache)
        log.info("--clean: build cache cleared.")

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

        ctx = hooksmod.build_hook_context(
            project, mode=effective_mode, verbose=verbose, workspace_dir=cfg.WORKSPACE
        )
        ok, pom_override, extra_mvn_args = hooksmod.run_hooks(
            "pre_build", [hooksmod.universal_prebuild], ctx
        )
        if not ok:
            log.error(f"Pre-build hook failed for: {project['name']}")
            return False

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
            log.error(f"Initial build failed at: {project['name']} — aborting.")
            return False

        hooksmod.run_hooks("post_build", [], ctx)

        if manifest is not None:
            hashermod.mark_built(
                Path(project["dir"]), manifest, all_manifests, effective_mode, effective_cache
            )
            invalidated = hashermod.invalidate_dependents(
                manifest.artifact_id, all_manifests, effective_cache
            )
            if invalidated:
                log.info(f"  cache invalidated for: {', '.join(invalidated)}")

    # ── Step 2: initial assemble ───────────────────────────────────────────
    if not runnermod._assemble_output(clean=False):
        log.error("Failed to assemble output directory.")
        return False

    # ── Step 3: launch CoffeeLoader ───────────────────────────────────────
    app = _AppProcess(java_opts=java_opts, env=env)
    if not app.start():
        return False

    # Give the JVM a moment to start
    time.sleep(1.5)

    # Initialise the REST API bridge (best-effort; no API key = no-op)
    bridge = _WatcherBridge()
    bridge.configure()

    # ── Step 4: watch loop ─────────────────────────────────────────────────
    log.section("Watching for changes  (Ctrl+C to stop)")

    stop_event = threading.Event()

    def _on_sigint(signum, frame):  # noqa: ANN001
        stop_event.set()

    signal.signal(signal.SIGINT,  _on_sigint)
    signal.signal(signal.SIGTERM, _on_sigint)

    try:
        while not stop_event.is_set():
            time.sleep(poll_interval)

            if stop_event.is_set():
                break

            if not app.is_alive():
                log.warn("[watch] Application exited — stopping watcher.")
                break

            # Detect changed projects
            changed_aids = hashermod.scan_changed(
                projects, all_manifests, effective_mode, effective_cache
            )
            if not changed_aids:
                continue

            changed_names = [
                all_manifests[aid].name for aid in changed_aids if aid in all_manifests
            ]
            log.info(f"[watch] Change detected: {', '.join(changed_names)}")

            # Debounce
            time.sleep(debounce)

            # Re-scan after debounce window
            changed_aids = hashermod.scan_changed(
                projects, all_manifests, effective_mode, effective_cache
            )
            if not changed_aids:
                continue

            # ── Rebuild ────────────────────────────────────────────────────
            log.section(f"[watch] Rebuilding {len(changed_aids)} project(s)…")
            rebuild_ok = _rebuild_projects(
                changed_aids,
                projects,
                all_manifests,
                mode=effective_mode,
                skip_tests=skip_tests,
                verbose=verbose,
                env=env,
                cache_dir=effective_cache,
            )

            if not rebuild_ok:
                log.warn("[watch] Rebuild had errors — keeping previous state.")
                continue

            # After rebuild the cascade may have added more aids to the cache;
            # re-read changed_aids to get the full set that was actually rebuilt
            # (cascade targets were also rebuilt inside _rebuild_projects).
            # We classify based on what was originally detected + cascaded.
            all_rebuilt_aids: List[str] = []
            for aid in changed_aids:
                all_rebuilt_aids.append(aid)
                m = all_manifests.get(aid)
                if m:
                    for dep_aid, dep_m in all_manifests.items():
                        if aid in {d.get("artifactId") for d in dep_m.workspace_deps}:
                            if dep_aid not in all_rebuilt_aids:
                                all_rebuilt_aids.append(dep_aid)

            # ── Classify → decide hot-swap vs relaunch ─────────────────────
            needs_relaunch, has_hot_swappable = _classify_changed(
                all_rebuilt_aids, all_manifests
            )

            # Collect source UUIDs that need quiescing (module jars only —
            # launcher / library changes require a full relaunch anyway, so
            # no point quiescing them through the API).
            module_source_uuids = [
                modLoader_src_uuid
                for aid in all_rebuilt_aids
                if (m := all_manifests.get(aid)) and m.module
                for modLoader_src_uuid in [None]  # resolved at runtime by CoffeeLoader
            ] if not needs_relaunch else []

            if not needs_relaunch:
                # Hot-swap path: quiesce modules → assemble → signal complete
                bridge.prepare_rebuild(source_uuids=[], module_uuids=[])
                runnermod._assemble_output(clean=False)
                bridge.rebuild_complete(source_uuids=[])
                log.success("[watch] Rebuild complete — hot-swap triggered.")
            else:
                # Relaunch path: CoffeeLoader will be killed anyway, so
                # just assemble then restart the process.
                runnermod._assemble_output(clean=False)
                log.info("[watch] Restart required — relaunching CoffeeLoader…")
                app.restart()
                # Re-configure bridge for new process (port may be same, but
                # forces fresh token on next hot-swap cycle).
                bridge.configure()
                # Give the fresh JVM a moment before the next poll
                time.sleep(1.5)

    finally:
        # ── Step 5: shutdown ───────────────────────────────────────────────
        log.info("\n[watch] Shutting down…")
        app.stop()
        log.info("[watch] Done.")

    return True

