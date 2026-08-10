"""Per-task git commits and rollback in the TARGET workspace.

Every completed task is committed locally in the target project's repo with
an [ai-sdlc:<task-id>] marker. Rolling a task back reverts exactly that
commit. Nothing is ever pushed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(workspace_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=workspace_root,
        capture_output=True,
        text=True,
    )


def commit_task(
    workspace_root: Path, task_id: str, message: str, paths: list[str] | None = None
) -> bool:
    """Commit the task's changes with the task marker. When paths are given,
    stage EXACTLY those files - so a task commit can never absorb unrelated
    or leftover changes. Returns False when there is nothing to commit or
    git is unavailable."""
    if paths:
        if _git(workspace_root, "add", "--", *paths).returncode != 0:
            return False
    else:
        if _git(workspace_root, "add", "-A").returncode != 0:
            return False
        # framework state is not part of the task's code change
        _git(workspace_root, "reset", "-q", "--", ".ai-sdlc")
    result = _git(
        workspace_root, "commit", "-q", "-m", f"[ai-sdlc:{task_id}] {message}"
    )
    return result.returncode == 0


def dirty_app_paths(workspace_root: Path, exclude_prefix: str = ".ai-sdlc") -> list[str]:
    """Uncommitted application paths (framework state excluded) - e.g.
    leftovers from an interrupted run."""
    result = _git(workspace_root, "status", "--porcelain")
    if result.returncode != 0:
        return []
    paths = []
    for line in result.stdout.splitlines():
        path = line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path and not path.startswith(exclude_prefix):
            paths.append(path)
    return sorted(paths)


def paths_dirty(workspace_root: Path, paths: list[str]) -> bool:
    """True when any of the given paths has uncommitted changes."""
    result = _git(workspace_root, "status", "--porcelain", "--", *paths)
    return result.returncode == 0 and bool(result.stdout.strip())


def commit_paths(workspace_root: Path, paths: list[str], task_id: str, message: str) -> bool:
    """Commit exactly the given paths with the [ai-sdlc:<id>] marker."""
    if _git(workspace_root, "add", "--", *paths).returncode != 0:
        return False
    result = _git(
        workspace_root, "commit", "-q", "-m", f"[ai-sdlc:{task_id}] {message}", "--", *paths
    )
    return result.returncode == 0


def rollback_task(workspace_root: Path, task_id: str) -> bool:
    """Revert the commit created for task_id. Returns False if not found."""
    log = _git(workspace_root, "log", "--format=%H %s")
    if log.returncode != 0:
        return False
    commit_hash = None
    for line in log.stdout.splitlines():
        sha, _, subject = line.partition(" ")
        if f"[ai-sdlc:{task_id}]" in subject:
            commit_hash = sha
            break
    if not commit_hash:
        return False
    result = _git(workspace_root, "revert", "--no-edit", commit_hash)
    return result.returncode == 0
