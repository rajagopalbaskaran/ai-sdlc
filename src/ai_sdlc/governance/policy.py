"""Policy guardrails checked on every task's output."""

from __future__ import annotations

import re
from pathlib import Path

SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*[\"'][^\"']{6,}[\"']"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
]


def check_policies(
    files_changed: list[str],
    workspace_root: Path,
    diff_limit: int = 500,
) -> list[str]:
    """Return a list of violations (empty = clean)."""
    violations: list[str] = []
    root = Path(workspace_root).resolve()
    total_lines = 0
    for name in files_changed:
        path = Path(name).resolve()
        if not path.is_relative_to(root):
            violations.append(f"{name}: write outside the workspace")
            continue
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        total_lines += content.count("\n")
        for pattern in SECRET_PATTERNS:
            if pattern.search(content):
                violations.append(f"{name}: possible secret/credential in code")
                break
    if total_lines > diff_limit:
        violations.append(
            f"change size {total_lines} lines exceeds limit {diff_limit}"
        )
    return violations
