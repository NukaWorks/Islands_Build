#!/usr/bin/env python3
"""
Islands Build CLI
=================

Usage examples
--------------
  python build.py build-all                          # build every project (tests skipped)
  python build.py build-all --with-tests             # build + run tests for every project
  python build.py build-all --java-version 24.0.2-tem
  python build.py run-islands                        # build all + assemble + launch
  python build.py run-islands --with-tests --no-clean --verbose
  python build.py run-islands --java-version 24.0.2-tem
  python build.py assemble                           # only assemble output dir (no build)
  python build.py clean                              # wipe the output dir
  python build.py status                             # show project/artifact status
  python build.py info                               # show resolved paths
  python build.py idea                               # generate IntelliJ IDEA project files
  python build.py idea --force                       # overwrite existing .iml files
  python build.py idea --java-version 24.0.2-tem     # target a specific JDK in IDEA
  python build.py sdk list                           # list installed Java candidates
  python build.py sdk install 24.0.2-tem             # install a Java candidate
  python build.py sdk use 24.0.2-tem                 # switch default Java candidate
"""

import argparse
import os
import sys
import time
import textwrap
import xml.etree.ElementTree as ET
from xml.dom import minidom

# ── make sure local modules are importable when run as a script ──────────────
sys.path.insert(0, os.path.dirname(__file__))

import config as cfg
import fs
import logger as log
import maven
import runner
import sdkman


# ─────────────────────────────────────────────────────────────────────────────
# Sub-command implementations
# ─────────────────────────────────────────────────────────────────────────────

def cmd_build_all(args: argparse.Namespace) -> int:
    """Build every project in dependency order."""
    skip_tests = not args.with_tests
    java_ver = args.java_version or cfg.JAVA_VERSION
    log.banner(
        "Build All",
        f"Tests: {'enabled' if args.with_tests else 'skipped'}  |  "
        f"Java: {java_ver or 'ambient'}  |  Verbose: {args.verbose}",
    )
    # Resolve env once so we fail early if Java is missing
    env = runner._resolve_env(java_ver)
    if env is None and java_ver:
        return 1

    total = len(cfg.PROJECTS)
    start = time.time()
    for i, project in enumerate(cfg.PROJECTS, 1):
        log.step(i, total, project["name"])
        ok = maven.build_project(
            project["name"],
            project["dir"],
            skip_tests=skip_tests,
            verbose=args.verbose,
            env=env,
        )
        if not ok:
            log.error(f"Build failed at: {project['name']}")
            return 1
    log.success(f"All {total} projects built in {log.duration(time.time() - start)}.")
    return 0


def cmd_run_islands(args: argparse.Namespace) -> int:
    """Build all projects then launch Islands via CoffeeLoader."""
    ok = runner.build_and_run_islands(
        skip_tests=not args.with_tests,
        clean_output=not args.no_clean,
        verbose=args.verbose,
        java_opts=args.java_opts,
        java_version=args.java_version,
    )
    return 0 if ok else 1


def cmd_assemble(args: argparse.Namespace) -> int:
    """Assemble the output directory without rebuilding (expects artifacts exist)."""
    log.banner("Assemble Output")
    ok = runner._assemble_output(clean=not args.no_clean)
    return 0 if ok else 1


def cmd_clean(args: argparse.Namespace) -> int:
    """Wipe the output directory."""
    log.banner("Clean Output")
    fs.clean_output(cfg.OUTPUT_DIR)
    log.success(f"Output directory cleaned: {cfg.OUTPUT_DIR}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Show whether each project's artifact exists."""
    log.banner("Project Status")

    rows = []
    for p in cfg.PROJECTS:
        art = p.get("artifact")
        if art:
            exists = art.exists()
            mark = "[green]✔[/green]" if exists else "[red]✖[/red]"
            rows.append((p["name"], str(art.name), mark, str(art.parent)))
        else:
            rows.append((p["name"], "—", "[dim]?[/dim]", "—"))

    try:
        from rich.table import Table
        from rich.console import Console
        table = Table(title="Maven Artifacts", show_lines=True)
        table.add_column("Project",  style="bold cyan",  no_wrap=True)
        table.add_column("Artifact", style="dim")
        table.add_column("Built",    justify="center")
        table.add_column("Location", style="dim", overflow="fold")
        for name, artifact, mark, location in rows:
            table.add_row(name, artifact, mark, location)
        Console().print(table)
    except ImportError:
        print(f"\n{'Project':<16}  {'Artifact':<45}  Built")
        print("─" * 80)
        for name, artifact, mark, location in rows:
            tick = "✔" if "green" in mark else "✖"
            print(f"{name:<16}  {artifact:<45}  {tick}")
        print()

    if cfg.OUTPUT_DIR.exists():
        jars = list(cfg.OUTPUT_DIR.rglob("*.jar"))
        log.info(f"Output dir: {cfg.OUTPUT_DIR}  ({len(jars)} jar(s))")
    else:
        log.warn(f"Output dir does not exist yet: {cfg.OUTPUT_DIR}")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    """Print resolved paths and Java configuration for this workspace."""
    log.banner("Build Configuration")

    # Java / sdkman info
    java_ver = cfg.JAVA_VERSION or "ambient (not configured)"
    log.info(f"   {'Configured Java':<22} {java_ver}")
    log.info(f"   {'Auto-install Java':<22} {cfg.AUTO_INSTALL_JAVA}")
    if sdkman.is_available():
        current = sdkman.current_candidate()
        cur_str = f"{current[0]}  ({current[1]})" if current else "none"
        log.info(f"   {'sdkman current Java':<22} {cur_str}")
    else:
        log.warn("   sdkman not found on this machine.")
    print()

    paths = {
        "Workspace":          cfg.WORKSPACE,
        "Build dir":          cfg.BUILD_DIR,
        "Output dir":         cfg.OUTPUT_DIR,
        "Modules dir":        cfg.MODULES_DIR,
        "ModularKit dir":     cfg.MODULARKIT_DIR,
        "CoffeeLoader dir":   cfg.COFFEELOADER_DIR,
        "Islands dir":        cfg.ISLANDS_DIR,
        "CoffeeLoader jar":   cfg.COFFEELOADER_OUTPUT_JAR,
        "Islands module jar": cfg.ISLANDS_MODULE_JAR,
    }
    for label, path in paths.items():
        exists = "✔" if path.exists() else "✖"
        log.info(f"{exists}  {label:<22} {path}")
    return 0


# ── sdk sub-commands ──────────────────────────────────────────────────────────

def cmd_sdk_list(args: argparse.Namespace) -> int:
    """List locally installed Java candidates known to sdkman."""
    log.banner("sdkman – Installed Java Candidates")
    if not sdkman.is_available():
        log.error("sdkman is not installed (expected at ~/.sdkman).")
        return 1
    sdkman.print_candidates()
    return 0


def cmd_sdk_install(args: argparse.Namespace) -> int:
    """Install a Java candidate via sdkman."""
    if not sdkman.is_available():
        log.error("sdkman is not installed (expected at ~/.sdkman).")
        return 1
    ok = sdkman.install(args.identifier)
    return 0 if ok else 1


def cmd_sdk_use(args: argparse.Namespace) -> int:
    """
    Switch the default sdkman Java candidate AND update JAVA_VERSION in config
    for this session. Prints the export command to make it permanent.
    """
    if not sdkman.is_available():
        log.error("sdkman is not installed (expected at ~/.sdkman).")
        return 1

    home = sdkman.resolve_java_home(args.identifier)
    if home is None:
        log.warn(f"{args.identifier} is not installed locally.")
        if args.install:
            log.info(f"Installing {args.identifier}…")
            if not sdkman.install(args.identifier):
                return 1
            home = sdkman.resolve_java_home(args.identifier)
        else:
            log.info("Pass --install to install it automatically.")
            return 1

    log.success(f"JAVA_HOME resolved: {home}")
    log.info(
        f"To make this permanent, set the environment variable:\n"
        f"  export ISLANDS_JAVA_VERSION={args.identifier}"
    )
    # Patch the in-process cfg so subsequent commands in the same run use it
    cfg.JAVA_VERSION = args.identifier
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# idea command – generate IntelliJ IDEA monorepo project files
# ─────────────────────────────────────────────────────────────────────────────

def _pretty_xml(element: ET.Element) -> str:
    """Return a pretty-printed XML string for the element."""
    raw = ET.tostring(element, encoding="unicode")
    dom = minidom.parseString(raw)
    lines = dom.toprettyxml(indent="  ").splitlines()
    # minidom adds an XML declaration on line 0; keep it
    return "\n".join(lines) + "\n"


def cmd_idea(args: argparse.Namespace) -> int:
    """Generate / refresh IntelliJ IDEA .idea project files for the monorepo."""
    log.banner("IDEA Project Setup", "Generating IntelliJ IDEA monorepo configuration")

    idea_dir = cfg.WORKSPACE / ".idea"
    idea_dir.mkdir(exist_ok=True)

    java_ver = args.java_version or cfg.JAVA_VERSION or "24"
    # Extract major version number only (e.g. "24.0.2-tem" → "24")
    java_major = java_ver.split(".")[0].split("-")[0]
    lang_level = f"JDK_{java_major}"

    # ── modules.xml ──────────────────────────────────────────────────────────
    # Each Maven project gets registered as a Maven module in IDEA
    maven_modules = [
        ("ModularKit",   cfg.MODULARKIT_DIR),
        ("CoffeeLoader", cfg.COFFEELOADER_DIR),
        ("Islands",      cfg.ISLANDS_DIR),
    ]

    project_el = ET.Element("project", version="4")
    mgr = ET.SubElement(project_el, "component", name="ProjectModuleManager")
    modules_el = ET.SubElement(mgr, "modules")

    # Build module (Python)
    build_iml = "$PROJECT_DIR$/Build/Build.iml"
    ET.SubElement(modules_el, "module",
                  fileurl=f"file://{build_iml}",
                  filepath=build_iml)

    # Root IDEA module
    root_iml = "$PROJECT_DIR$/.idea/Islands.iml"
    ET.SubElement(modules_el, "module",
                  fileurl=f"file://{root_iml}",
                  filepath=root_iml)

    # Maven sub-projects
    for name, project_dir in maven_modules:
        rel = project_dir.relative_to(cfg.WORKSPACE)
        iml_rel = f"$PROJECT_DIR$/{rel}/{name}.iml"
        ET.SubElement(modules_el, "module",
                      fileurl=f"file://{iml_rel}",
                      filepath=iml_rel,
                      group="Maven Projects")

    modules_xml_path = idea_dir / "modules.xml"
    modules_xml_path.write_text(_pretty_xml(project_el), encoding="utf-8")
    log.success(f"Written: {modules_xml_path.relative_to(cfg.WORKSPACE)}")

    # ── misc.xml ─────────────────────────────────────────────────────────────
    misc_el = ET.Element("project", version="4")
    ET.SubElement(misc_el, "component",
                  name="Black",
                  **{"option": ""})  # placeholder
    # Rebuild without placeholder
    misc_el = ET.Element("project", version="4")
    black = ET.SubElement(misc_el, "component", name="Black")
    opt = ET.SubElement(black, "option", name="sdkName")
    opt.set("value", "Python 3.13")
    root_mgr = ET.SubElement(misc_el, "component",
                              name="ProjectRootManager",
                              version="2",
                              languageLevel=lang_level,
                              **{"project-jdk-name": f"Java {java_major}",
                                 "project-jdk-type": "JavaSDK"})
    ET.SubElement(root_mgr, "output", url="file://$PROJECT_DIR$/out")

    misc_xml_path = idea_dir / "misc.xml"
    misc_xml_path.write_text(_pretty_xml(misc_el), encoding="utf-8")
    log.success(f"Written: {misc_xml_path.relative_to(cfg.WORKSPACE)}")

    # ── .iml files for each Maven sub-project ────────────────────────────────
    for name, project_dir in maven_modules:
        iml_path = project_dir / f"{name}.iml"
        if iml_path.exists() and not args.force:
            log.info(f"Skipping (already exists): {iml_path.relative_to(cfg.WORKSPACE)}")
            continue
        iml_el = ET.Element("module",
                             type="JAVA_MODULE",
                             version="4")
        root_mgr_el = ET.SubElement(iml_el, "component",
                                     name="NewModuleRootManager",
                                     **{"inherit-compiler-output": "true"})
        ET.SubElement(root_mgr_el, "exclude-output")
        content = ET.SubElement(root_mgr_el, "content",
                                 url="file://$MODULE_DIR$")
        pom = project_dir / "pom.xml"
        if pom.exists():
            ET.SubElement(content, "excludeFolder",
                          url="file://$MODULE_DIR$/target")
        ET.SubElement(root_mgr_el, "orderEntry",
                      type="inheritedJdk")
        ET.SubElement(root_mgr_el, "orderEntry",
                      type="sourceFolder", forTests="false")
        iml_path.write_text(_pretty_xml(iml_el), encoding="utf-8")
        log.success(f"Written: {iml_path.relative_to(cfg.WORKSPACE)}")

    # ── vcs.xml ──────────────────────────────────────────────────────────────
    vcs_path = idea_dir / "vcs.xml"
    if not vcs_path.exists() or args.force:
        vcs_el = ET.Element("project", version="4")
        vcs_comp = ET.SubElement(vcs_el, "component", name="VcsDirectoryMappings")
        ET.SubElement(vcs_comp, "mapping",
                      directory="$PROJECT_DIR$",
                      vcs="Git")
        vcs_path.write_text(_pretty_xml(vcs_el), encoding="utf-8")
        log.success(f"Written: {vcs_path.relative_to(cfg.WORKSPACE)}")

    # ── encodings.xml ─────────────────────────────────────────────────────────
    enc_path = idea_dir / "encodings.xml"
    if not enc_path.exists() or args.force:
        enc_el = ET.Element("project", version="4")
        enc_comp = ET.SubElement(enc_el, "component",
                                  name="Encoding",
                                  addBOMForNewFiles=";UTF-8:with NO BOM",
                                  defaultCharsetForPropertiesFiles="UTF-8")
        enc_path.write_text(_pretty_xml(enc_el), encoding="utf-8")
        log.success(f"Written: {enc_path.relative_to(cfg.WORKSPACE)}")

    # ── compiler.xml ──────────────────────────────────────────────────────────
    compiler_path = idea_dir / "compiler.xml"
    comp_el = ET.Element("project", version="4")
    compiler_comp = ET.SubElement(comp_el, "component", name="CompilerConfiguration")
    ET.SubElement(compiler_comp, "bytecodeTargetLevel",
                  **{"target": java_major})
    compiler_path.write_text(_pretty_xml(comp_el), encoding="utf-8")
    log.success(f"Written: {compiler_path.relative_to(cfg.WORKSPACE)}")

    log.banner(
        "Done",
        textwrap.dedent(f"""\
        Open the workspace root in IntelliJ IDEA:
          File → Open → {cfg.WORKSPACE}
        Then: File → Project Structure → Modules to verify all {len(maven_modules)} modules.
        If prompted, import Maven projects from the pom.xml files in each module.""")
    )
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# CLI parser
# ─────────────────────────────────────────────────────────────────────────────

def _add_java_version_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--java-version", metavar="ID", default=None,
        help=(
            f"sdkman Java candidate to use, e.g. '24.0.2-tem' "
            f"(default: {cfg.JAVA_VERSION or 'ambient PATH'}). "
            "Overrides ISLANDS_JAVA_VERSION env var."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build",
        description="Islands Build Automation CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version="islands-build 1.0.0")

    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    # ── build-all ─────────────────────────────────────────────────────────────
    p_build = sub.add_parser(
        "build-all",
        help="Build all projects (ModularKit → CoffeeLoader → Islands)",
        description="Build every project in dependency order using 'mvn clean install'.",
    )
    p_build.add_argument("--with-tests", action="store_true",
        help="Run unit tests during build (default: tests are skipped)")
    p_build.add_argument("--verbose", "-v", action="store_true",
        help="Show full Maven output (removes --batch-mode)")
    _add_java_version_arg(p_build)
    p_build.set_defaults(func=cmd_build_all)

    # ── run-islands ───────────────────────────────────────────────────────────
    p_run = sub.add_parser(
        "run-islands",
        help="Build all, assemble output dir, then launch Islands via CoffeeLoader",
        description=(
            "Full pipeline:\n"
            "  1. mvn clean install  ModularKit\n"
            "  2. mvn clean install  CoffeeLoader\n"
            "  3. mvn clean install  Islands\n"
            "  4. Assemble output/ directory\n"
            "  5. Write CoffeeLoader config.json  (sources → output/modules/)\n"
            "  6. java -jar CoffeeLoader          (blocks until Ctrl+C)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_run.add_argument("--with-tests", action="store_true",
        help="Run unit tests during build")
    p_run.add_argument("--no-clean", action="store_true",
        help="Do not wipe the output directory before assembling")
    p_run.add_argument("--verbose", "-v", action="store_true",
        help="Show full Maven output")
    p_run.add_argument("--java-opts", metavar="OPTS", default=None,
        help='Extra JVM options before -jar, e.g. "--java-opts \"-Xmx512m\""')
    _add_java_version_arg(p_run)
    p_run.set_defaults(func=cmd_run_islands)

    # ── assemble ──────────────────────────────────────────────────────────────
    p_asm = sub.add_parser(
        "assemble",
        help="Copy built artifacts into output/ without rebuilding",
    )
    p_asm.add_argument("--no-clean", action="store_true",
        help="Keep existing output dir contents")
    p_asm.set_defaults(func=cmd_assemble)

    # ── clean ─────────────────────────────────────────────────────────────────
    p_clean = sub.add_parser("clean", help="Delete the output/ directory")
    p_clean.set_defaults(func=cmd_clean)

    # ── status ────────────────────────────────────────────────────────────────
    p_status = sub.add_parser("status", help="Show build status of each Maven artifact")
    p_status.set_defaults(func=cmd_status)

    # ── info ──────────────────────────────────────────────────────────────────
    p_info = sub.add_parser("info", help="Print resolved workspace paths and Java config")
    p_info.set_defaults(func=cmd_info)

    # ── idea ──────────────────────────────────────────────────────────────────
    p_idea = sub.add_parser(
        "idea",
        help="Generate / refresh IntelliJ IDEA .idea monorepo project files",
        description=(
            "Creates / updates .idea/modules.xml, .idea/misc.xml, .idea/compiler.xml,\n"
            ".idea/vcs.xml, .idea/encodings.xml and a <Name>.iml for each Maven module,\n"
            "so the workspace root can be opened as a single IDEA project containing\n"
            "ModularKit, CoffeeLoader and Islands as module entries."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_idea.add_argument(
        "--force", "-f", action="store_true",
        help="Overwrite existing .iml and helper files (default: skip if present)",
    )
    _add_java_version_arg(p_idea)
    p_idea.set_defaults(func=cmd_idea)

    # ── sdk ───────────────────────────────────────────────────────────────────
    p_sdk = sub.add_parser(
        "sdk",
        help="Manage Java installations via sdkman",
        description="Manage Java installations via sdkman.",
    )
    sdk_sub = p_sdk.add_subparsers(dest="sdk_command", metavar="<sdk-command>")
    sdk_sub.required = True

    # sdk list
    p_sdk_list = sdk_sub.add_parser("list", help="List locally installed Java candidates")
    p_sdk_list.set_defaults(func=cmd_sdk_list)

    # sdk install <id>
    p_sdk_inst = sdk_sub.add_parser(
        "install",
        help="Install a Java candidate  (e.g. 24.0.2-tem)",
    )
    p_sdk_inst.add_argument("identifier", metavar="IDENTIFIER",
        help="sdkman candidate identifier, e.g. 24.0.2-tem")
    p_sdk_inst.set_defaults(func=cmd_sdk_install)

    # sdk use <id>
    p_sdk_use = sdk_sub.add_parser(
        "use",
        help="Switch the active Java candidate for subsequent build commands",
    )
    p_sdk_use.add_argument("identifier", metavar="IDENTIFIER",
        help="sdkman candidate identifier, e.g. 24.0.2-tem")
    p_sdk_use.add_argument("--install", action="store_true",
        help="Install the candidate first if it is not already available")
    p_sdk_use.set_defaults(func=cmd_sdk_use)

    return parser


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()

