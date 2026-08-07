"""Task dependency graph resolution."""

from __future__ import annotations

from ai_sdlc.state.plan import Task


class CycleError(Exception):
    """The task graph contains a cycle or an unknown dependency."""


def detect_cycles(tasks: list[Task]) -> None:
    """Raise CycleError on cycles or references to unknown task ids."""
    ids = {t.id for t in tasks}
    for task in tasks:
        for dep in task.depends_on:
            if dep not in ids:
                raise CycleError(f"task {task.id} depends on unknown task {dep}")
    # Kahn's algorithm: if we cannot order every node, a cycle exists.
    indegree = {t.id: 0 for t in tasks}
    dependents: dict[str, list[str]] = {t.id: [] for t in tasks}
    for task in tasks:
        indegree[task.id] = len(task.depends_on)
        for dep in task.depends_on:
            dependents[dep].append(task.id)
    queue = [tid for tid, deg in indegree.items() if deg == 0]
    seen = 0
    while queue:
        tid = queue.pop()
        seen += 1
        for child in dependents[tid]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if seen != len(tasks):
        cyclic = sorted(tid for tid, deg in indegree.items() if deg > 0)
        raise CycleError(f"dependency cycle involving: {', '.join(cyclic)}")


def eligible_tasks(tasks: list[Task]) -> list[Task]:
    """Pending tasks whose dependencies are all completed, in plan order."""
    done = {t.id for t in tasks if t.status == "completed"}
    return [
        t
        for t in tasks
        if t.status == "pending" and all(dep in done for dep in t.depends_on)
    ]


def terminal(tasks: list[Task]) -> bool:
    """True when no further progress is possible: nothing eligible and
    nothing currently in flight."""
    if eligible_tasks(tasks):
        return False
    return not any(t.status in ("in_progress", "waiting_approval") for t in tasks)
