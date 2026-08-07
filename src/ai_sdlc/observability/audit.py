"""Append-only JSONL audit log - the tamper-evident record of a run."""

from __future__ import annotations

import json
import time
from pathlib import Path


class AuditLog:
    def __init__(self, runs_dir: Path, run_id: str):
        self.run_id = run_id
        self.path = Path(runs_dir) / f"audit-{run_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, type: str, **fields) -> None:
        """Append one event. The file is never rewritten or truncated."""
        record = {"ts": time.time(), "run_id": self.run_id, "type": type, **fields}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
