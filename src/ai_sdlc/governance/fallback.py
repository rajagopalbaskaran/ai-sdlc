"""Adapter fallback chain: if the primary AI tool fails, try the next."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from ai_sdlc.adapters.base import Adapter, AdapterResult

if TYPE_CHECKING:
    from ai_sdlc.state.plan import Task


class FallbackChain(Adapter):
    name = "fallback-chain"

    def __init__(self, adapters: list[Adapter], on_fallback: Callable[..., None] | None = None):
        if not adapters:
            raise ValueError("fallback chain needs at least one adapter")
        self.adapters = adapters
        self.on_fallback = on_fallback

    def execute(self, persona: str, context: str, task: "Task") -> AdapterResult:
        result = AdapterResult(ok=False, error="no adapters")
        for index, adapter in enumerate(self.adapters):
            result = adapter.execute(persona, context, task)
            if result.ok:
                return result
            if index < len(self.adapters) - 1:
                if self.on_fallback:
                    self.on_fallback(
                        task=task.id,
                        from_adapter=adapter.name,
                        to_adapter=self.adapters[index + 1].name,
                        error=result.error,
                    )
        return result
