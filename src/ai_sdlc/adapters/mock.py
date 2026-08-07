"""Deterministic fake adapter for tests, offline demos, and fallback.

Behaves like an AI tool that answers instantly with scripted results,
including failing on command - which is exactly what testing retry,
fallback, and rollback logic requires.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ai_sdlc.adapters.base import Adapter, AdapterResult

if TYPE_CHECKING:
    from ai_sdlc.state.plan import Task


class MockAdapter(Adapter):
    name = "mock"

    def __init__(self, script: dict[str, list[AdapterResult]] | None = None):
        """script maps task id -> queue of results returned in order.
        Tasks without a script entry succeed with a canned output."""
        self._script = {k: list(v) for k, v in (script or {}).items()}

    def execute(self, persona: str, context: str, task: "Task") -> AdapterResult:
        queue = self._script.get(task.id)
        if queue:
            return queue.pop(0)
        return AdapterResult(
            ok=True,
            output=f"[mock:{persona}] completed task {task.id}: {task.title}",
        )
