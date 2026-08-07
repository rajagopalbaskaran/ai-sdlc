"""Entry and exit gates for pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field

from ai_sdlc.state.plan import Task

STAGES = ("analyze", "plan", "develop", "validate", "test", "deploy_ready")


@dataclass
class GateResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class GateContext:
    tasks: list[Task] = field(default_factory=list)
    approvals: dict[str, bool] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)


def entry_gate(stage: str, ctx: GateContext) -> GateResult:
    reasons: list[str] = []
    if stage not in STAGES:
        return GateResult(False, [f"unknown stage {stage!r}"])
    if stage == "develop":
        if not ctx.tasks:
            reasons.append("no tasks in implementation plan")
        if not ctx.approvals.get("plan"):
            reasons.append("implementation plan has not been approved")
    return GateResult(not reasons, reasons)


def exit_gate(stage: str, ctx: GateContext) -> GateResult:
    reasons: list[str] = []
    if stage not in STAGES:
        return GateResult(False, [f"unknown stage {stage!r}"])
    if stage == "develop":
        for task in ctx.tasks:
            if task.status != "completed":
                reasons.append(f"task {task.id} is {task.status}")
    if stage == "deploy_ready" and not ctx.approvals.get("deploy_ready"):
        reasons.append("deploy readiness has not been approved")
    return GateResult(not reasons, reasons)
