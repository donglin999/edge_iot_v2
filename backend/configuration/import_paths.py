"""Helpers for locating Excel import files (M12).

Uploaded import files used to be referenced by an *absolute* path stored in
``ImportJob.summary["file_path"]``. That breaks the moment the project is moved
or run from a container with a different ``BASE_DIR``. We now store the path
*relative to* ``BASE_DIR`` and resolve it on read; absolute legacy values are
still accepted so old jobs keep working.
"""
from __future__ import annotations

from pathlib import Path

from django.conf import settings

# All import uploads live here.
IMPORT_STORAGE_SUBDIR = Path("uploads") / "import_jobs"


def import_storage_dir() -> Path:
    """Absolute directory that holds uploaded import files."""
    return Path(settings.BASE_DIR) / IMPORT_STORAGE_SUBDIR


def to_relative(path: str | Path) -> str:
    """Return *path* as a POSIX string relative to ``BASE_DIR`` when possible.

    A path outside ``BASE_DIR`` (or already relative) is returned unchanged.
    """
    p = Path(path)
    base = Path(settings.BASE_DIR)
    try:
        return p.relative_to(base).as_posix()
    except ValueError:
        return p.as_posix()


def resolve(stored: str | Path) -> Path:
    """Resolve a stored ``file_path`` back to an absolute path.

    Accepts both the new relative form and legacy absolute paths.
    """
    p = Path(stored)
    if p.is_absolute():
        return p
    return Path(settings.BASE_DIR) / p
