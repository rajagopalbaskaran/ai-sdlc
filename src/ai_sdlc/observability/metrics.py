"""Reliability metrics computed from the audit log."""

from __future__ import annotations

import json
from pathlib import Path


def _load(jsonl_path: Path) -> list[dict]:
    events = []
    for line in Path(jsonl_path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def compute_metrics(jsonl_path: Path) -> dict:
    events = _load(jsonl_path)
    started = {e["task"] for e in events if e["type"] == "task_started"}
    completed = {e["task"] for e in events if e["type"] == "task_completed"}
    retries = sum(1 for e in events if e["type"] == "retry")
    rollbacks = sum(1 for e in events if e["type"] == "rollback")

    # MTTR: mean seconds from a task's failure to its next completion.
    recovery_times: list[float] = []
    open_failures: dict[str, float] = {}
    for event in events:
        if event["type"] == "task_failed":
            open_failures.setdefault(event["task"], event["ts"])
        elif event["type"] == "task_completed" and event.get("task") in open_failures:
            recovery_times.append(event["ts"] - open_failures.pop(event["task"]))

    run_start = next((e["ts"] for e in events if e["type"] == "run_started"), None)
    run_end = next(
        (e["ts"] for e in reversed(events) if e["type"] in ("run_completed", "run_stopped")),
        None,
    )

    # per-stage latency: first stage_started to last stage_completed per stage
    stage_start: dict[str, float] = {}
    per_stage: dict[str, float] = {}
    for event in events:
        if event["type"] == "stage_started":
            stage_start.setdefault(event["stage"], event["ts"])
        elif event["type"] == "stage_completed" and event.get("stage") in stage_start:
            per_stage[event["stage"]] = event["ts"] - stage_start[event["stage"]]

    return {
        "tasks_started": len(started),
        "tasks_completed": len(completed),
        "success_rate": (len(completed) / len(started)) if started else 1.0,
        "retries": retries,
        "rollbacks": rollbacks,
        "mttr_seconds": (sum(recovery_times) / len(recovery_times)) if recovery_times else 0.0,
        "e2e_seconds": (run_end - run_start) if run_start is not None and run_end is not None else 0.0,
        "per_stage_seconds": per_stage,
    }
