"""Render audit log and metrics as human-readable markdown."""

from __future__ import annotations

import json
from pathlib import Path

from ai_sdlc.observability.metrics import compute_metrics


def render_report(jsonl_path: Path) -> str:
    metrics = compute_metrics(jsonl_path)
    lines = [
        "# Run Report",
        "",
        "## Reliability Metrics",
        "",
        f"- Success rate: {metrics['success_rate'] * 100:.1f}%",
        f"- Tasks started/completed: {metrics['tasks_started']}/{metrics['tasks_completed']}",
        f"- Retries: {metrics['retries']}",
        f"- Rollbacks: {metrics['rollbacks']}",
        f"- MTTR: {metrics['mttr_seconds']:.1f}s",
        f"- End-to-end: {metrics['e2e_seconds']:.1f}s",
    ]
    if metrics["per_stage_seconds"]:
        lines += ["", "## Latency by stage", ""]
        lines += [
            f"- {stage}: {seconds:.1f}s"
            for stage, seconds in metrics["per_stage_seconds"].items()
        ]

    decision_lines = []
    for line in Path(jsonl_path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        event = json.loads(line)
        if event["type"] == "decision":
            subject = event.get("subject", event.get("kind", "?"))
            choice = event.get("choice", "")
            reasons = event.get("reasons") or []
            suffix = f" ({'; '.join(str(r) for r in reasons)})" if reasons else ""
            decision_lines.append(f"- {subject}: {choice}{suffix}".rstrip())
        elif event["type"] == "approval":
            decision_lines.append(f"- approval {event.get('gate')}: {event.get('decision')}")
    if decision_lines:
        lines += ["", "## Decisions", ""] + decision_lines

    lines += ["", "## Timeline", ""]
    for line in Path(jsonl_path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        event = json.loads(line)
        detail = " ".join(
            f"{k}={v}" for k, v in event.items() if k not in ("ts", "run_id", "type")
        )
        lines.append(f"- [{event['ts']:.1f}] {event['type']} {detail}".rstrip())
    lines.append("")
    return "\n".join(lines)
