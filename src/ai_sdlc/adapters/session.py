"""Adapter for interactive (IDE) mode.

In session mode the agent is the interactive Claude Code session driving the
CLI, not a subprocess the engine spawns. This adapter exists so Engine can be
constructed with the same contract; calling execute() means orchestration
took the headless path by mistake, which is a bug worth failing loudly on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ai_sdlc.adapters.base import Adapter, AdapterResult

if TYPE_CHECKING:
    from ai_sdlc.state.plan import Task


class SessionAdapter(Adapter):
    name = "session"

    def execute(self, persona: str, context: str, task: "Task") -> AdapterResult:
        raise RuntimeError(
            "session adapter cannot execute tasks: the interactive session performs "
            "the work - drive it with 'ai-sdlc next' and 'ai-sdlc report-task'"
        )
