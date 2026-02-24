"""
File-system helpers: copy artifacts, create the output layout, write config.
"""
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

import logger as log


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    log.info(f"Directory ready: {path}")


def copy_artifact(src: Path, dst: Path) -> bool:
    """Copy *src* jar to *dst* (file path, not directory)."""
    if not src.exists():
        log.error(f"Artifact not found: {src}")
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    log.success(f"Copied  {src.name}  →  {dst}")
    return True


def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    log.success(f"Wrote config: {path}")


def copy_config(
    src: Path,
    dst: Path,
    *,
    sources_override: Optional[List[str]] = None,
) -> bool:
    """
    Copy *src* ``config.json`` to *dst*, optionally replacing the ``sources``
    field with *sources_override* so the runtime always points at the correct
    modules directory.

    Returns True on success, False if *src* does not exist or cannot be read.
    """
    if not src.exists():
        log.error(f"Source config not found: {src}")
        return False
    try:
        with open(src, "r", encoding="utf-8") as fh:
            data: Dict[str, Any] = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log.error(f"Failed to read source config {src}: {exc}")
        return False

    if sources_override is not None:
        data["sources"] = sources_override

    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    log.success(f"Copied config  {src.name}  →  {dst}")
    return True


def clean_output(output_dir: Path) -> None:
    if output_dir.exists():
        shutil.rmtree(output_dir)
        log.info(f"Cleaned output directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    log.info(f"Created fresh output directory: {output_dir}")

