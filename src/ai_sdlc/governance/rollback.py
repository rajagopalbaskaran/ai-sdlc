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


def commit_task(workspace_root: Path, task_id: str, message: str) -> bool:
    """Stage everything and commit with the task marker. Returns False when
    there is nothing to commit or git is unavailable."""
    if _git(workspace_root, "add", "-A").returncode != 0:
        return False
    result = _git(
        workspace_root, "commit", "-q", "-m", f"[ai-sdlc:{task_id}] {message}"
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
