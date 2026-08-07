"""Bounded retries with failure context fed back into the next attempt."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from ai_sdlc.adapters.base import AdapterResult

if TYPE_CHECKING:
    from ai_sdlc.state.plan import Task


class RetryPolicy:
    def __init__(self, budget: int = 2):
        self.budget = budget

    def attempt(
        self,
        task: "Task",
        fn: Callable[[str | None], AdapterResult],
        on_retry: Callable[..., None] | None = None,
    ) -> AdapterResult:
        """Call fn up to budget+1 times. fn receives the previous attempt's
        error (None on the first try) so the agent can learn from failure."""
        last_error: str | None = None
        result = AdapterResult(ok=False, error="not attempted")
        for attempt in range(self.budget + 1):
            result = fn(last_error)
            if result.ok:
                return result
            last_error = result.error or "unknown error"
            if attempt < self.budget:
                if on_retry:
                    on_retry(task=task.id, attempt=attempt + 1, error=last_error)
        return result
