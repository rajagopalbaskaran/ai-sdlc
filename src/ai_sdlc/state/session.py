"""Session state: identity for an interactive (IDE-driven) run.

Headless runs hold run identity in one Engine object for the whole run.
Session mode spans many separate CLI processes - `ai-sdlc next` and
`ai-sdlc report-task` are different invocations - so run id, retry counters,
the pre-task file snapshot, and the pinned branch must live on disk between
calls. This file is orchestrator-owned state; the interactive session never
writes it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml


@dataclass
class SessionState:
    run_id: str
    started_at: float
    branch: str | None = None
    active_task: str | None = None
    attempts: dict[str, int] = field(default_factory=dict)
    last_error: dict[str, str] = field(default_factory=dict)
    snapshot: dict[str, list[int]] = field(default_factory=dict)

    @classmethod
    def load(cls, ws) -> "SessionState | None":
        path = ws.session_path
        if not path.is_file():
            return None
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict) or "run_id" not in data:
            return None
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, ws) -> None:
        ws.session_path.parent.mkdir(parents=True, exist_ok=True)
        ws.session_path.write_text(
            yaml.safe_dump(asdict(self), sort_keys=False), encoding="utf-8"
        )

    @classmethod
    def clear(cls, ws) -> bool:
        """Remove the session file. Returns False when there was none."""
        path: Path = ws.session_path
        if not path.is_file():
            return False
        path.unlink()
        return True
