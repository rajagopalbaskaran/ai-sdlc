"""ai-sdlc command line interface."""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

import yaml

from ai_sdlc.adapters.base import build_adapter
from ai_sdlc.changes import diff, snapshot
from ai_sdlc.governance.approvals import request_approval
from ai_sdlc.governance.branching import current_branch, push_branch
from ai_sdlc.governance.fallback import FallbackChain
from ai_sdlc.governance.rollback import rollback_task
from ai_sdlc.observability.audit import AuditLog
from ai_sdlc.observability.metrics import compute_metrics
from ai_sdlc.observability.report import render_report
from ai_sdlc.observability.summary import generate_summary
from ai_sdlc.orchestrator.engine import Engine
from ai_sdlc.orchestrator.replan import extract_header, merge, render_plan
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


def _warn_unexpected_changes(ws: Workspace, audit, stage: str, before) -> None:
    """Text stages (analyze/plan/replan) must not modify the workspace.
    Any detected change is audited and surfaced - defense in depth on top of
    the adapter's permission denial."""
    changed = diff(before, snapshot(ws.root))
    if changed:
        audit.event("policy_warning", stage=stage, count=len(changed), files=changed[:20])
        print(
            f"warning: {stage} stage unexpectedly changed {len(changed)} file(s); see audit log",
            file=sys.stderr,
        )


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
    engine = Engine(ws, _build_engine_adapter(ws, config), config, echo=True)
    print(f"analyzing requirement via {config.get('adapter', 'mock')} (may take a few minutes; Ctrl+C is safe)...")
    task = Task(
        id="ANALYZE",
        title="Requirement analysis",
        status="pending",
        persona="requirement_analyst",
        body=requirement,
    )
    engine.audit.event("stage_started", stage="analyze")
    engine.audit.event("task_started", task="ANALYZE", persona="requirement_analyst")
    before = snapshot(ws.root)
    result = engine.adapter.execute("requirement_analyst", engine._context_for(task), task)
    _warn_unexpected_changes(ws, engine.audit, "analyze", before)
    if not result.ok:
        engine.audit.event("task_failed", task="ANALYZE", error=result.error or "unknown")
        print(f"analysis failed: {result.error}", file=sys.stderr)
        return 1
    out = ws.state_dir / "plan" / "requirement-analysis.md"
    out.write_text(result.output, encoding="utf-8")
    engine.audit.event("task_completed", task="ANALYZE", artifact=str(out))
    engine.audit.event("stage_completed", stage="analyze")
    print(f"analysis written to {out}")
    return 0


def cmd_plan(args) -> int:
    ws = _require_workspace(args.workspace)
    config = _load_config(ws)
    analysis_path = ws.state_dir / "plan" / "requirement-analysis.md"
    if not analysis_path.is_file():
        print("error: no requirement-analysis.md; run: ai-sdlc analyze first", file=sys.stderr)
        return 1
    engine = Engine(ws, _build_engine_adapter(ws, config), config, echo=True)
    print(f"planning via {config.get('adapter', 'mock')} (may take a few minutes; Ctrl+C is safe)...")
    task = Task(
        id="PLAN",
        title="Implementation planning",
        status="pending",
        persona="implementation_planner",
        body=analysis_path.read_text(encoding="utf-8"),
    )
    engine.audit.event("stage_started", stage="plan")
    engine.audit.event("task_started", task="PLAN", persona="implementation_planner")
    before = snapshot(ws.root)
    result = engine.adapter.execute("implementation_planner", engine._context_for(task), task)
    _warn_unexpected_changes(ws, engine.audit, "plan", before)
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
    engine.audit.event("stage_completed", stage="plan")
    print("review the plan, then run: ai-sdlc develop (plan approval will be requested)")
    return 0


def cmd_run(args) -> int:
    ws = _require_workspace(args.workspace)
    config = _load_config(ws)
    engine = Engine(ws, _build_engine_adapter(ws, config), config, echo=True)
    parallel = True if args.parallel else None
    summary = engine.run(parallel=parallel, retry_blocked=getattr(args, "retry_blocked", False))
    print(
        f"run {engine.run_id}: {summary.status} "
        f"(completed={summary.completed} blocked={summary.blocked} rolled_back={summary.rolled_back})"
    )
    print(f"audit: {engine.audit.path}")
    if summary.blocked:
        print("blocked tasks:")
        for task_id, reason in engine.blocked_report():
            print(f"  {task_id} - {reason or 'see audit log'}")
        print("fix the causes, then rerun: ai-sdlc develop (it will offer to retry them)")
        print("if the plan itself is wrong rather than the execution: ai-sdlc replan")
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


def cmd_replan(args) -> int:
    ws = _require_workspace(args.workspace)
    config = _load_config(ws)
    doc = PlanDocument.load(ws.plan_path)
    current_text = ws.plan_path.read_text(encoding="utf-8")
    audit = AuditLog(ws.runs_dir, time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    audit.event("replan_started", requirement=args.requirement, proposal=args.proposal)

    # 1) optionally refresh the analysis from a changed requirement
    if args.requirement:
        engine = Engine(ws, _build_engine_adapter(ws, config), config, audit=audit)
        task = Task(
            id="REANALYZE",
            title="Re-analysis after requirement change",
            status="pending",
            persona="requirement_analyst",
            body=Path(args.requirement).read_text(encoding="utf-8"),
        )
        before = snapshot(ws.root)
        result = engine.adapter.execute("requirement_analyst", engine._context_for(task), task)
        _warn_unexpected_changes(ws, audit, "replan-analyze", before)
        if not result.ok:
            print(f"re-analysis failed: {result.error}", file=sys.stderr)
            return 1
        ws.analysis_path.write_text(result.output, encoding="utf-8")
        audit.event("task_completed", task="REANALYZE", artifact=str(ws.analysis_path))

    # 2) obtain the proposed revised task set
    if args.proposal:
        proposal_text = Path(args.proposal).read_text(encoding="utf-8")
    else:
        engine = Engine(ws, _build_engine_adapter(ws, config), config, audit=audit)
        analysis = (
            ws.analysis_path.read_text(encoding="utf-8") if ws.analysis_path.is_file() else ""
        )
        task = Task(
            id="REPLAN",
            title="Revise the implementation plan",
            status="pending",
            persona="implementation_planner",
            body=(
                "The requirement/analysis changed while the plan below is mid-execution.\n"
                "Propose the full revised task set (yaml blocks). Completed tasks are protected.\n\n"
                f"## Current analysis\n\n{analysis}\n\n## Current plan\n\n{current_text}"
            ),
        )
        before = snapshot(ws.root)
        result = engine.adapter.execute("implementation_planner", engine._context_for(task), task)
        _warn_unexpected_changes(ws, audit, "replan-propose", before)
        if not result.ok:
            print(f"re-planning failed: {result.error}", file=sys.stderr)
            return 1
        proposal_text = result.output

    proposed = PlanDocument(ws.plan_path, proposal_text).tasks
    diff = merge(doc.tasks, proposed)
    print(diff.summary())
    audit.event(
        "decision",
        subject="replan_diff",
        choice=diff.summary(),
        reasons=["requirement/analysis changed while the plan was mid-execution"],
        keep=diff.keep,
        unchanged=diff.unchanged,
        revised=diff.revised,
        dropped=diff.dropped,
        added=diff.added,
    )

    # 3) governed application: the human approves the diff
    decision = "approve" if args.yes else request_approval("Apply the revised plan?")
    audit.event("approval", gate="replan", decision=decision)
    if decision != "approve":
        print("replan not approved; plan unchanged")
        return 1

    ws.plan_path.write_text(render_plan(extract_header(current_text), diff.merged), encoding="utf-8")

    # revised plan = re-approved plan, pinned to the current analysis
    approvals_path = ws.state_dir / "approvals.yaml"
    approvals = {}
    if approvals_path.is_file():
        approvals = yaml.safe_load(approvals_path.read_text(encoding="utf-8")) or {}
    approvals["plan"] = True
    sha = ws.analysis_sha()
    if sha:
        approvals["analysis_sha"] = sha
    approvals_path.write_text(yaml.safe_dump(approvals), encoding="utf-8")

    audit.event("replan_applied", tasks=len(diff.merged))
    print(f"revised plan written to {ws.plan_path}; continue with: ai-sdlc develop")
    return 0


def cmd_retry(args) -> int:
    """Human decision: a blocked task's cause was addressed - make it
    eligible again so continue/develop picks it up."""
    ws = _require_workspace(args.workspace)
    doc = PlanDocument.load(ws.plan_path)
    try:
        task = doc.get(args.task_id)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if task.status not in ("blocked", "rolled_back"):
        print(
            f"error: task {task.id} is {task.status}; only blocked or rolled_back tasks can be retried",
            file=sys.stderr,
        )
        return 1
    audit = AuditLog(ws.runs_dir, time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    doc.set_status(task.id, "pending")
    doc.save()
    audit.event(
        "decision",
        subject="task_retry",
        choice=f"reset {task.id} to pending",
        reasons=["human confirmed the blocking cause was addressed"],
    )
    print(f"task {task.id} reset to pending; resume with: ai-sdlc develop")
    return 0


def cmd_summarize(args) -> int:
    ws = _require_workspace(args.workspace)
    markdown = generate_summary(ws)
    out = ws.state_dir / "engineering-summary.md"
    out.write_text(markdown, encoding="utf-8")
    print(f"engineering summary written to {out}")
    return 0


def cmd_push(args) -> int:
    ws = _require_workspace(args.workspace)
    doc = PlanDocument.load(ws.plan_path)
    branch = doc.meta.get("branch") or current_branch(ws.root)
    if not branch:
        print("error: no branch recorded in the plan and none checked out", file=sys.stderr)
        return 1
    audit = AuditLog(ws.runs_dir, time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    decision = "approve" if args.yes else request_approval(f"Push branch {branch} to origin?")
    audit.event("approval", gate="push", decision=decision)
    if decision != "approve":
        print("push not approved; nothing sent")
        return 1
    pushed, detail = push_branch(ws.root, branch)
    audit.event("push", branch=branch, ok=pushed, detail=detail)
    if not pushed:
        print(f"push failed: {detail}", file=sys.stderr)
        return 1
    print(f"pushed {branch} to origin")
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

    p_dev = sub.add_parser(
        "develop",
        aliases=["run"],
        parents=[common],
        help="execute the implementation plan - safe to run anytime, always continues from current state (alias: run)",
    )
    p_dev.add_argument("--parallel", action="store_true", help="run independent tasks concurrently")
    p_dev.add_argument(
        "--retry-blocked",
        action="store_true",
        help="re-authorize blocked tasks without prompting (for non-interactive use)",
    )

    sub.add_parser("status", parents=[common], help="show task states")
    sub.add_parser("report", parents=[common], help="render audit log and metrics to markdown")

    p_rollback = sub.add_parser("rollback", parents=[common], help="revert one task's commit and mark it rolled_back")
    p_rollback.add_argument("task_id", help="task id to roll back (e.g. T3)")
    p_rollback.add_argument("--yes", action="store_true", help="skip the confirmation prompt")

    p_replan = sub.add_parser(
        "replan", parents=[common], help="absorb a requirement change: diff, revise pending tasks, re-approve"
    )
    p_replan.add_argument("requirement", nargs="?", default=None, help="changed requirement file (re-runs analysis)")
    p_replan.add_argument("--proposal", default=None, help="use a prepared proposal file instead of the planner agent")
    p_replan.add_argument("--yes", action="store_true", help="skip the diff approval prompt")

    p_push = sub.add_parser("push", parents=[common], help="push the plan's feature branch to origin (human-gated)")
    p_push.add_argument("--yes", action="store_true", help="skip the confirmation prompt")

    p_retry = sub.add_parser("retry", parents=[common], help="reset a blocked task to pending after fixing its cause")
    p_retry.add_argument("task_id", help="task id to make eligible again (e.g. T3)")

    sub.add_parser("summarize", parents=[common], help="generate the engineering summary from project state")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    handlers = {
        "init": cmd_init,
        "analyze": cmd_analyze,
        "plan": cmd_plan,
        "develop": cmd_run,
        "run": cmd_run,
        "status": cmd_status,
        "report": cmd_report,
        "rollback": cmd_rollback,
        "replan": cmd_replan,
        "push": cmd_push,
        "retry": cmd_retry,
        "summarize": cmd_summarize,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
