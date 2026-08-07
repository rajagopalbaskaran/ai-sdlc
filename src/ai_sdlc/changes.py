"""Workspace change detection.

Snapshots the file tree before a task runs and diffs it afterwards, so the
framework knows exactly which files an agent touched - even when the AI tool
does not report its own changes. Feeds the policy guardrails and the
per-task commit.
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_IGNORE = (".ai-sdlc", ".git")

Snapshot = dict[str, tuple[int, int]]


def snapshot(root: Path, ignore: tuple[str, ...] = DEFAULT_IGNORE) -> Snapshot:
    """Map of file path -> (mtime_ns, size) for every file under root,
    excluding framework state and git internals."""
    root = Path(root)
    state: Snapshot = {}
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] in ignore:
            continue
        if path.is_file():
            stat = path.stat()
            state[str(path)] = (stat.st_mtime_ns, stat.st_size)
    return state


def diff(before: Snapshot, after: Snapshot) -> list[str]:
    """Paths added, modified, or deleted between two snapshots."""
    changed = [path for path, sig in after.items() if before.get(path) != sig]
    changed.extend(path for path in before if path not in after)
    return sorted(changed)
