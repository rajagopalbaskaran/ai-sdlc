"""Knowledge base loading."""

from __future__ import annotations

from pathlib import Path


def load_kb(state_dir: Path) -> dict[str, str]:
    """Return {relative_name: content} for every markdown doc in the KB."""
    kb_dir = Path(state_dir) / "knowledge-base"
    if not kb_dir.is_dir():
        return {}
    docs: dict[str, str] = {}
    for path in sorted(kb_dir.rglob("*.md")):
        docs[str(path.relative_to(kb_dir))] = path.read_text(encoding="utf-8")
    return docs
