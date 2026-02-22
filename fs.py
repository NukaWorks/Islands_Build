"""
File-system helpers: copy artifacts, create the output layout, write config.
"""
import json
import shutil
from pathlib import Path
from typing import Any, Dict

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


def clean_output(output_dir: Path) -> None:
    if output_dir.exists():
        shutil.rmtree(output_dir)
        log.info(f"Cleaned output directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    log.info(f"Created fresh output directory: {output_dir}")

