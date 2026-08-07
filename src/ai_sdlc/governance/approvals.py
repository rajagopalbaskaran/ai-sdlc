"""Human approval checkpoints - the CLI gate where humans stay in control."""

from __future__ import annotations

from typing import Callable, Literal

Decision = Literal["approve", "reject", "modify"]

_ANSWERS: dict[str, Decision] = {
    "a": "approve",
    "approve": "approve",
    "r": "reject",
    "reject": "reject",
    "m": "modify",
    "modify": "modify",
}


def request_approval(prompt: str, input_fn: Callable[[str], str] = input) -> Decision:
    """Ask the human to approve/reject/modify. EOF (non-interactive or
    Ctrl+Z/Ctrl+D) is treated as reject - never approve by default."""
    while True:
        try:
            raw = input_fn(f"{prompt} [a]pprove / [r]eject / [m]odify: ")
        except (EOFError, OSError):
            # no interactive stdin: never approve by default
            return "reject"
        decision = _ANSWERS.get(raw.strip().lower())
        if decision:
            return decision
        print("please answer a, r, or m")
