"""ai-sdlc command line interface."""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

import yaml

from ai_sdlc.adapters.base import build_adapter
from ai_sdlc.governance.approvals import request_approval
from ai_sdlc.governance.fallback import FallbackChain
from ai_sdlc.governance.rollback import rollback_task
from ai_sdlc.observability.audit import AuditLog
from ai_sdlc.observability.metrics import compute_metrics
from ai_sdlc.observability.report import render_report
from ai_sdlc.orchestrator.engine import Engine
from ai_sdlc.state.plan import PlanDocument, Task
from ai_sdlc.workspace import Workspace


def _load_config(ws: Workspace) -> dict:
    path = ws.state_dir / "config.yaml"
    if path.is_file():
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    else:
        config = {}
    config.setdefault("workdir", str(ws.root))
    return config


def _build_engine_adapter(ws: Workspace, config: dict, audit_event=None):
    primary = build_adapter(config.get("adapter", "mock"), config)
    fallbacks = [build_adapter(name, config) for name in config.get("fallback_adapters", [])]
    if not fallbacks:
        return primary
    return FallbackChain([primary, *fallbacks], on_fallback=audit_event)


def _require_workspace(root: Path) -> Workspace:
    ws = Workspace(root)
    if not ws.exists():
        raise SystemExit(f"error: {ws.state_dir} not found; run: ai-sdlc init --workspace {root}")
    return ws


def cmd_init(args) -> int:
    try:
        ws = Workspace.init(args.workspace)
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"initialized {ws.state_dir}")
    print("next: fill in project-profile.md and knowledge-base/, then run: ai-sdlc analyze <requirement-file>")
    return 0


def cmd_analyze(args) -> int:
    ws = _require_workspace(args.workspace)
    config = _load_config(ws)
    requirement = Path(args.requirement).read_text(encoding="utf-8")
    engine = Engine(ws, _build_engine_adapter(ws, config), config)
    task = Task(
        id="ANALYZE",
        title="Requirement analysis",
        status="pending",
        persona="requirement_analyst",
        body=requirement,
    )
    engine.audit.event("task_started", task="ANALYZE", persona="requirement_analyst")
    result = engine.adapter.execute("requirement_analyst", engine._context_for(task), task)
    if not result.ok:
        engine.audit.event("task_failed", task="ANALYZE", error=result.error or "unknown")
        print(f"analysis failed: {result.error}", file=sys.stderr)
        return 1
    out = ws.state_dir / "plan" / "requirement-analysis.md"
    out.write_text(result.output, encoding="utf-8")
    engine.audit.event("task_completed", task="ANALYZE", artifact=str(out))
    print(f"analysis written to {out}")
    return 0


def cmd_plan(args) -> int:
    ws = _require_workspace(args.workspace)
    config = _load_config(ws)
    analysis_path = ws.state_dir / "plan" / "requirement-analysis.md"
    if not analysis_path.is_file():
        print("error: no requirement-analysis.md; run: ai-sdlc analyze first", file=sys.stderr)
        return 1
    engine = Engine(ws, _build_engine_adapter(ws, config), config)
    task = Task(
        id="PLAN",
        title="Implementation planning",
        status="pending",
        persona="implementation_planner",
        body=analysis_path.read_text(encoding="utf-8"),
    )
    engine.audit.event("task_started", task="PLAN", persona="implementation_planner")
    result = engine.adapter.execute("implementation_planner", engine._context_for(task), task)
    if not result.ok:
        engine.audit.event("task_failed", task="PLAN", error=result.error or "unknown")
        print(f"planning failed: {result.error}", file=sys.stderr)
        return 1
    if "```yaml" in result.output:
        with ws.plan_path.open("a", encoding="utf-8") as handle:
            handle.write("\n" + result.output.strip() + "\n")
        print(f"tasks appended to {ws.plan_path}")
    else:
        print("planner produced no task blocks; plan file unchanged (see audit log)")
    engine.audit.event("task_completed", task="PLAN")
    print("review the plan, then run: ai-sdlc run (plan approval will be requested)")
    return 0


def cmd_run(args) -> int:
    ws = _require_workspace(args.workspace)
    config = _load_config(ws)
    engine = Engine(ws, _build_engine_adapter(ws, config), config)
    parallel = True if args.parallel else None
    summary = engine.run(parallel=parallel)
    print(
        f"run {engine.run_id}: {summary.status} "
        f"(completed={summary.completed} blocked={summary.blocked} rolled_back={summary.rolled_back})"
    )
    print(f"audit: {engine.audit.path}")
    return 0 if summary.status == "completed" else 1


def cmd_status(args) -> int:
    ws = _require_workspace(args.workspace)
    doc = PlanDocument.load(ws.plan_path)
    if not doc.tasks:
        print("no tasks in the implementation plan yet")
        return 0
    width = max(len(t.id) for t in doc.tasks)
    for task in doc.tasks:
        deps = ",".join(task.depends_on) or "-"
        print(f"{task.id:<{width}}  {task.status:<16}  persona={task.persona:<24} deps={deps}  {task.title}")
    return 0


def cmd_rollback(args) -> int:
    ws = _require_workspace(args.workspace)
    doc = PlanDocument.load(ws.plan_path)
    try:
        task = doc.get(args.task_id)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    audit = AuditLog(ws.runs_dir, time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    if args.yes:
        decision = "approve"
    else:
        decision = request_approval(
            f"Roll back task {task.id} ({task.title})? This reverts its commit"
        )
    audit.event("approval", gate=f"rollback:{task.id}", decision=decision)
    if decision != "approve":
        print("rollback not approved; nothing changed")
        return 1
    if not rollback_task(ws.root, task.id):
        audit.event("rollback", task=task.id, ok=False, error="no [ai-sdlc] commit found or revert failed")
        print(f"error: could not revert task {task.id} (no marked commit found, or revert conflict)", file=sys.stderr)
        return 1
    doc.set_status(task.id, "rolled_back")
    doc.save()
    audit.event("rollback", task=task.id, ok=True)
    print(f"task {task.id} rolled back (git revert) and marked rolled_back")
    return 0


def cmd_report(args) -> int:
    ws = _require_workspace(args.workspace)
    logs = sorted(ws.runs_dir.glob("audit-*.jsonl"))
    if not logs:
        print("no runs recorded yet")
        return 0
    latest = logs[-1]
    markdown = render_report(latest)
    out = latest.with_name(latest.stem.replace("audit-", "report-") + ".md")
    out.write_text(markdown, encoding="utf-8")
    metrics = compute_metrics(latest)
    print(f"report written to {out}")
    print(
        f"success_rate={metrics['success_rate'] * 100:.1f}% retries={metrics['retries']} "
        f"rollbacks={metrics['rollbacks']} mttr={metrics['mttr_seconds']:.1f}s "
        f"e2e={metrics['e2e_seconds']:.1f}s"
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="target project workspace (default: current directory)",
    )
    parser = argparse.ArgumentParser(
        prog="ai-sdlc",
        description="Spec-driven, agent-executed SDLC framework under human governance",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", parents=[common], help="plant .ai-sdlc state folder into the workspace")

    p_analyze = sub.add_parser("analyze", parents=[common], help="normalize a requirement into an analysis")
    p_analyze.add_argument("requirement", help="path to the requirement file")

    sub.add_parser("plan", parents=[common], help="turn the analysis into an implementation plan")

    p_run = sub.add_parser("run", parents=[common], help="execute the implementation plan")
    p_run.add_argument("--parallel", action="store_true", help="run independent tasks concurrently")

    p_cont = sub.add_parser("continue", parents=[common], help="resume execution from current state")
    p_cont.add_argument("--parallel", action="store_true", help="run independent tasks concurrently")

    sub.add_parser("status", parents=[common], help="show task states")
    sub.add_parser("report", parents=[common], help="render audit log and metrics to markdown")

    p_rollback = sub.add_parser("rollback", parents=[common], help="revert one task's commit and mark it rolled_back")
    p_rollback.add_argument("task_id", help="task id to roll back (e.g. T3)")
    p_rollback.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    handlers = {
        "init": cmd_init,
        "analyze": cmd_analyze,
        "plan": cmd_plan,
        "run": cmd_run,
        "continue": cmd_run,
        "status": cmd_status,
        "report": cmd_report,
        "rollback": cmd_rollback,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
