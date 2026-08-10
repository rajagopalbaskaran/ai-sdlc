"""Feature-branch lifecycle in the TARGET workspace.

Agents work on a feature branch recorded in the plan; main stays clean.
Pushing is a separate, human-gated action - never automatic.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def current_branch(root: Path) -> str | None:
    result = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def checkout_branch(root: Path, name: str) -> bool:
    """Switch to the branch, creating it from the current HEAD if needed."""
    exists = _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}")
    if exists.returncode == 0:
        return _git(root, "checkout", "-q", name).returncode == 0
    return _git(root, "checkout", "-q", "-b", name).returncode == 0


def has_remote(root: Path) -> bool:
    result = _git(root, "remote")
    return result.returncode == 0 and bool(result.stdout.strip())


def remote_url(root: Path, name: str = "origin") -> str | None:
    result = _git(root, "remote", "get-url", name)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def set_remote(root: Path, url: str, name: str = "origin") -> bool:
    """Point `name` at `url`, adding the remote when it does not exist yet.

    Projects the framework generates start with no origin at all, so without
    this there is nothing for push_branch to publish to."""
    if remote_url(root, name) is None:
        return _git(root, "remote", "add", name, url).returncode == 0
    return _git(root, "remote", "set-url", name, url).returncode == 0


def push_branch(root: Path, name: str) -> tuple[bool, str]:
    """Push the branch to origin. Returns (ok, message)."""
    result = _git(root, "push", "-u", "origin", name)
    message = (result.stderr or result.stdout).strip()
    return result.returncode == 0, message
