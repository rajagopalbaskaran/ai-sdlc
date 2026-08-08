"""Append-only JSONL audit log - the tamper-evident record of a run."""

from __future__ import annotations

import json
import time
from pathlib import Path


def _short(value, limit: int = 90) -> str:
    text = str(value).replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _friendly(type: str, f: dict) -> str | None:
    """Human-readable one-liner for events worth showing live; None = silent."""
    task = f.get("task", "")
    if type == "task_started":
        title = f" - {_short(f['title'])}" if f.get("title") else ""
        return f"-> {task} started{title} ({f.get('persona', '')})"
    if type == "task_completed":
        extras = []
        if "files_changed" in f:
            extras.append(f"{f['files_changed']} files")
        if "seconds" in f:
            extras.append(f"{f['seconds']}s")
        detail = f" ({', '.join(extras)})" if extras else ""
        return f"   {task} completed{detail}"
    if type == "task_failed":
        return f"   {task} FAILED: {_short(f.get('error', ''))}"
    if type == "retry":
        return f"   {task} retrying (attempt {f.get('attempt', '?')})"
    if type == "fallback":
        return f"   {task} switching adapter {f.get('from_adapter')} -> {f.get('to_adapter')}"
    if type == "commit" and f.get("committed"):
        return f"   {task} committed"
    if type == "branch":
        return f"working on branch {f.get('name')}"
    if type == "push":
        return f"push {'ok' if f.get('ok') else 'FAILED'}: {f.get('branch')}"
    return None


class AuditLog:
    def __init__(self, runs_dir: Path, run_id: str, echo: bool = False):
        self.run_id = run_id
        self.echo = echo
        self.path = Path(runs_dir) / f"audit-{run_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, type: str, **fields) -> None:
        """Append one event. The file is never rewritten or truncated.
        With echo on, key events also print live as simple one-liners."""
        record = {"ts": time.time(), "run_id": self.run_id, "type": type, **fields}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        if self.echo:
            line = _friendly(type, fields)
            if line:
                print(line, flush=True)
