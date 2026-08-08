"""Dynamic re-planning: absorb a requirement change mid-flight.

Rules:
- Completed and rolled_back tasks are protected - the proposal can never
  rewrite history.
- Pending / blocked / waiting tasks may be revised (reset to pending),
  dropped, or joined by new tasks.
- The merged plan must still be a valid DAG.

The human approves the resulting diff before it is written - re-planning
is governed, never silent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import yaml

from ai_sdlc.orchestrator.dag import detect_cycles
from ai_sdlc.state.plan import Task

PROTECTED_STATUSES = ("completed", "rolled_back")


@dataclass
class PlanDiff:
    keep: list[str] = field(default_factory=list)       # protected, untouched
    unchanged: list[str] = field(default_factory=list)  # same in proposal
    revised: list[str] = field(default_factory=list)    # replaced by proposal
    dropped: list[str] = field(default_factory=list)    # removed by proposal
    added: list[str] = field(default_factory=list)      # new in proposal
    merged: list[Task] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"keep: {', '.join(self.keep) or '-'} | "
            f"unchanged: {', '.join(self.unchanged) or '-'} | "
            f"revised: {', '.join(self.revised) or '-'} | "
            f"dropped: {', '.join(self.dropped) or '-'} | "
            f"added: {', '.join(self.added) or '-'}"
        )


def _same(a: Task, b: Task) -> bool:
    return (
        a.body == b.body
        and a.depends_on == b.depends_on
        and a.persona == b.persona
        and a.title == b.title
    )


def merge(current: list[Task], proposed: list[Task]) -> PlanDiff:
    diff = PlanDiff()
    proposed_by_id = {t.id: t for t in proposed}
    current_ids = {t.id for t in current}

    for task in current:
        if task.status in PROTECTED_STATUSES:
            diff.keep.append(task.id)
            diff.merged.append(task)
            continue
        replacement = proposed_by_id.get(task.id)
        if replacement is None:
            diff.dropped.append(task.id)
            continue
        if _same(task, replacement) and task.status == "pending":
            diff.unchanged.append(task.id)
            diff.merged.append(task)
        else:
            replacement.status = "pending"
            diff.revised.append(task.id)
            diff.merged.append(replacement)

    for task in proposed:
        if task.id not in current_ids:
            task.status = "pending"
            diff.added.append(task.id)
            diff.merged.append(task)

    detect_cycles(diff.merged)
    return diff


def extract_header(plan_text: str) -> str:
    """Everything before the first task heading."""
    marker = "\n### "
    index = plan_text.find(marker)
    if index == -1:
        return plan_text
    return plan_text[: index + 1]


def render_plan(header: str, tasks: list[Task]) -> str:
    parts = [header.rstrip() + "\n"]
    for task in tasks:
        data = {
            "id": task.id,
            "status": task.status,
            "depends_on": task.depends_on,
            "persona": task.persona,
            "artifacts": task.artifacts,
            "derived_from": task.derived_from,
        }
        dumped = yaml.safe_dump(data, sort_keys=False, default_flow_style=None).strip()
        parts.append(f"\n### {task.title}\n\n```yaml\n{dumped}\n```\n\n{task.body}\n")
    return "".join(parts)
