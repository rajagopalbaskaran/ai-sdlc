"""Adapter interface: how the framework talks to any AI coding tool."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ai_sdlc.state.plan import Task


@dataclass
class AdapterResult:
    ok: bool
    output: str = ""
    files_changed: list[str] = field(default_factory=list)
    error: str | None = None


class Adapter(ABC):
    """One fixed contract; each AI tool gets a translator implementing it."""

    name: str = "base"

    @abstractmethod
    def execute(self, persona: str, context: str, task: "Task") -> AdapterResult:
        """Run one persona on one task with the given context."""


def build_adapter(name: str, config: dict) -> Adapter:
    from ai_sdlc.adapters.claude_code import ClaudeCodeAdapter
    from ai_sdlc.adapters.mock import MockAdapter

    if name == "mock":
        return MockAdapter()
    if name == "claude-code":
        return ClaudeCodeAdapter(
            command=config.get("claude_command", "claude"),
            timeout=config.get("task_timeout_seconds", 600),
            workdir=config.get("workdir"),
            persona_permissions=config.get("persona_permissions") or {},
        )
    raise ValueError(f"unknown adapter {name!r}; available: mock, claude-code")
