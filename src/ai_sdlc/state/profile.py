"""Project profile loading."""

from __future__ import annotations

from pathlib import Path


def load_profile(state_dir: Path) -> str:
    """Return the project profile markdown, or empty string if absent."""
    path = Path(state_dir) / "project-profile.md"
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")
