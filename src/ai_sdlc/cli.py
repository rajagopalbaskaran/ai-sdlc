"""ai-sdlc command line interface."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

import yaml

from ai_sdlc.adapters.base import build_adapter
from ai_sdlc.changes import diff, snapshot
from ai_sdlc.cli_session import cmd_branch, cmd_remote
from ai_sdlc.governance.approvals import request_approval
from ai_sdlc.governance.branching import current_branch, has_remote, push_branch
from ai_sdlc.governance.fallback import FallbackChain
from ai_sdlc.governance.rollback import commit_paths, paths_dirty, rollback_task
from ai_sdlc.observability.audit import AuditLog
from ai_sdlc.observability.metrics import compute_metrics
from ai_sdlc.observability.report import render_report
from ai_sdlc.observability.summary import generate_summary
from ai_sdlc.orchestrator.dag import CycleError
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


def _revoke_plan_approval(ws: Workspace, audit, reason: str) -> bool:
    """The plan changed relative to what was approved - the approval must be
    re-earned. Returns True when an active approval was actually revoked."""
    approvals_path = ws.state_dir / "approvals.yaml"
    if not approvals_path.is_file():
        return False
    approvals = yaml.safe_load(approvals_path.read_text(encoding="utf-8")) or {}
    if not approvals.get("plan"):
        return False
    approvals["plan"] = False
    approvals.pop("analysis_sha", None)
    # downstream sign-offs derived from the old plan fall with it
    approvals.pop("deploy_ready", None)
    approvals_path.write_text(yaml.safe_dump(approvals), encoding="utf-8")
    audit.event(
        "decision",
        subject="plan_approval_revoked",
        choice="re-approval required",
        reasons=[reason],
    )
    return True


def _commit_snapshot(
    ws: Workspace,
    config: dict,
    audit,
    paths: list[str],
    marker: str,
    message: str,
    enforce: bool = False,
) -> bool:
    """Version stage artifacts so downstream stages derive from git-frozen
    inputs. Returns False only when enforce=True and a human explicitly
    declines in ask mode."""
    if not (ws.root / ".git").is_dir():
        print("note: workspace is not a git repository - stage artifacts are not versioned")
        return True

    def _has_content(path: Path) -> bool:
        # empty directories break git commit pathspecs and hold nothing to version
        if path.is_file():
            return True
        return path.is_dir() and any(f.is_file() for f in path.rglob("*"))

    paths = [p for p in paths if _has_content(ws.root / p)]
    if not paths or not paths_dirty(ws.root, paths):
        return True
    mode = config.get("commit_mode", "auto")
    if mode == "off":
        print(f"note: commit_mode off - {marker} snapshot not committed")
        return True
    if mode == "ask":
        decision = request_approval(f"Commit the {marker} snapshot?")
        audit.event("approval", gate=f"{marker}_snapshot", decision=decision)
        if decision != "approve":
            if enforce:
                print(
                    f"this step requires a versioned {marker} snapshot; commit it "
                    "yourself or approve the snapshot",
                    file=sys.stderr,
                )
                return False
            print(f"note: {marker} snapshot skipped by human decision")
            return True
    committed = commit_paths(ws.root, paths, marker, message)
    audit.event("commit", task=f"{marker}-snapshot", committed=committed, files=paths)
    if committed:
        print(f"committed {marker} snapshot ({', '.join(paths)})")
    else:
        print(f"warning: {marker} snapshot commit failed; see git status", file=sys.stderr)
    return True


def _upstream_paths(ws: Workspace) -> list[str]:
    paths: list[str] = []
    if ws.analysis_path.is_file():
        paths.append(str(ws.analysis_path.relative_to(ws.root)))
    requirement = PlanDocument.load(ws.plan_path).meta.get("requirement")
    if requirement and (ws.root / requirement).is_file():
        paths.append(requirement)
    return paths


def _commit_upstream(ws: Workspace, config: dict, audit) -> bool:
    """Plan-start gate: the (reviewed) requirement and analysis must be
    committed before planning derives tasks from them."""
    return _commit_snapshot(
        ws,
        config,
        audit,
        _upstream_paths(ws),
        "requirement",
        "reviewed requirement and analysis snapshot for planning",
        enforce=True,
    )


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

    # a new analysis invalidates any prior plan approval: the plan must be
    # re-approved against the upstream it now derives from
    if _revoke_plan_approval(
        ws,
        engine.audit,
        "a new requirement analysis replaced the one the plan was approved against",
    ):
        print("note: prior plan approval revoked - develop will ask for approval again")
        # release the branch pin too: new requirement, new branch
        doc = PlanDocument.load(ws.plan_path)
        if doc.meta.get("branch"):
            old_branch = doc.meta["branch"]
            doc.set_meta(branch=None)
            doc.save()
            engine.audit.event(
                "decision",
                subject="branch_unpinned",
                choice=f"released {old_branch}",
                reasons=["a new requirement gets its own branch"],
            )

    # remember which requirement file this analysis came from, so planning
    # can version both together
    doc = PlanDocument.load(ws.plan_path)
    try:
        requirement_rel = str(Path(args.requirement).resolve().relative_to(ws.root.resolve()))
    except ValueError:
        requirement_rel = str(args.requirement)
    doc.set_meta(requirement=requirement_rel)
    doc.save()

    # freeze what the agent saw (requirement, profile, knowledge base) and
    # what it concluded (raw analysis) in one snapshot; the plan-start
    # snapshot later captures the human-reviewed version - the diff between
    # the two is evidence of human review
    _commit_snapshot(
        ws,
        config,
        engine.audit,
        [
            requirement_rel,
            ".ai-sdlc/project-profile.md",
            ".ai-sdlc/knowledge-base",
            str(ws.analysis_path.relative_to(ws.root)),
        ],
        "analysis",
        "requirement, profile, and raw analysis",
    )

    print(f"analysis written to {out}")
    print("next: review the analysis (answer/adjust ambiguities), then run: ai-sdlc plan")
    return 0


def cmd_plan(args) -> int:
    ws = _require_workspace(args.workspace)
    config = _load_config(ws)
    analysis_path = ws.state_dir / "plan" / "requirement-analysis.md"
    if not analysis_path.is_file():
        print("error: no requirement-analysis.md; run: ai-sdlc analyze first", file=sys.stderr)
        return 1
    engine = Engine(ws, _build_engine_adapter(ws, config), config, echo=True)
    if not _commit_upstream(ws, config, engine.audit):
        return 1
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
        # new tasks were never approved: any prior approval no longer covers them
        if _revoke_plan_approval(
            ws, engine.audit, "new tasks were appended after the plan was approved"
        ):
            print("note: plan changed - re-approval will be requested at develop")
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
    try:
        summary = engine.run(parallel=parallel, retry_blocked=getattr(args, "retry_blocked", False))
    except CycleError as exc:
        print(f"error: the implementation plan is invalid - {exc}", file=sys.stderr)
        print(f"fix the plan file, then rerun: {ws.plan_path}", file=sys.stderr)
        return 1
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
    elif summary.status == "completed":
        print(
            "next: ai-sdlc test (run the suites), then: ai-sdlc validate "
            "(requirement-to-code check); publish with: ai-sdlc push"
        )
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

    if not _commit_upstream(ws, config, audit):
        return 1

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


def cmd_test(args) -> int:
    """Execute the project's declared test commands - mechanical, no LLM.
    Proves the code passes its own tests; validate proves it meets the
    requirement. Both feed the release decision."""
    ws = _require_workspace(args.workspace)
    config = _load_config(ws)
    commands = config.get("test_commands") or []
    if not commands:
        print("no test_commands configured in .ai-sdlc/config.yaml", file=sys.stderr)
        print(
            'example:\n  test_commands:\n    - "cd backend && mvn test"\n    - "cd frontend && npm test"',
            file=sys.stderr,
        )
        return 1
    audit = AuditLog(ws.runs_dir, time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    audit.event("stage_started", stage="test")
    all_ok = True
    for command in commands:
        print(f"running: {command}")
        started = time.time()
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=ws.root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=config.get("test_timeout_seconds", 1800),
            )
            ok = proc.returncode == 0
            tail = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()[-2000:]
        except subprocess.TimeoutExpired:
            ok, tail = False, "timed out"
        seconds = round(time.time() - started)
        audit.event("test_command", command=command, ok=ok, seconds=seconds, output_tail=tail)
        print(f"  {'PASS' if ok else 'FAIL'} ({seconds}s)")
        if not ok:
            all_ok = False
            print(tail[-1200:])
    audit.event("stage_completed", stage="test")
    audit.event(
        "decision",
        subject="test_stage",
        choice="pass" if all_ok else "fail",
        reasons=[f"{len(commands)} command(s) executed"],
    )
    if all_ok:
        print("all test commands passed")
        print("next: ai-sdlc validate (requirement-to-code check), then: ai-sdlc push")
        return 0
    print("test failures - fix and rerun: ai-sdlc test", file=sys.stderr)
    return 1


def cmd_validate(args) -> int:
    """Requirement-to-code validation: the Validator agent reads the original
    requirement and the actual code, and reports MET/PARTIAL/MISSING per
    requirement item with evidence."""
    ws = _require_workspace(args.workspace)
    config = _load_config(ws)
    doc = PlanDocument.load(ws.plan_path)
    requirement_rel = doc.meta.get("requirement")
    if requirement_rel and (ws.root / requirement_rel).is_file():
        requirement_text = (ws.root / requirement_rel).read_text(encoding="utf-8")
    elif ws.analysis_path.is_file():
        requirement_text = ws.analysis_path.read_text(encoding="utf-8")
    else:
        print("error: no requirement or analysis found to validate against", file=sys.stderr)
        return 1

    # latest automated-test evidence from the audit history
    test_evidence: list[str] = []
    for log in sorted(ws.runs_dir.glob("audit-*.jsonl")):
        for line in log.read_text(encoding="utf-8").splitlines():
            if '"test_command"' in line:
                event = json.loads(line)
                test_evidence.append(
                    f"- {event.get('command')}: {'PASS' if event.get('ok') else 'FAIL'}"
                )

    engine = Engine(ws, _build_engine_adapter(ws, config), config, echo=True)
    body = (
        "Validate the implemented code in this workspace against the ORIGINAL "
        "requirement below. Produce a markdown validation report containing:\n"
        "- a table: | Requirement item | Verdict | Evidence |\n"
        "  where Verdict is exactly one of MET, PARTIAL, MISSING\n"
        "- cover every requirement line; do not skip any\n"
        "- cite actual file paths as evidence\n"
        "- NEVER claim a test passed unless it appears as PASS in the automated\n"
        "  test results below; a test's existence in the code is not evidence it\n"
        "  runs or passes - if no executed result covers a claim, say so\n"
        "- end with a line: Summary: X met, Y partial, Z missing\n"
        "Read the code; do not modify anything.\n\n"
        "## Latest automated test results\n\n"
        + ("\n".join(test_evidence[-20:]) or "- none recorded (consider: ai-sdlc test)")
        + f"\n\n## Original requirement\n\n{requirement_text}"
    )
    task = Task(
        id="VALIDATE",
        title="Requirement-to-code validation",
        status="pending",
        persona="validator",
        body=body,
    )
    print(f"validating via {config.get('adapter', 'mock')} (may take a few minutes; Ctrl+C is safe)...")
    engine.audit.event("stage_started", stage="validate")
    engine.audit.event("task_started", task="VALIDATE", persona="validator")
    before = snapshot(ws.root)
    result = engine.adapter.execute("validator", engine._context_for(task), task)
    _warn_unexpected_changes(ws, engine.audit, "validate", before)
    if not result.ok:
        engine.audit.event("task_failed", task="VALIDATE", error=result.error or "unknown")
        print(f"validation failed to run: {result.error}", file=sys.stderr)
        return 1
    report = ws.state_dir / "plan" / "validation-report.md"
    report.write_text(result.output, encoding="utf-8")
    engine.audit.event("task_completed", task="VALIDATE", artifact=str(report))
    engine.audit.event("stage_completed", stage="validate")
    _commit_snapshot(
        ws,
        config,
        engine.audit,
        [str(report.relative_to(ws.root))],
        "validation",
        "requirement-to-code validation report",
    )
    print(f"validation report written to {report}")
    if "MISSING" in result.output or "PARTIAL" in result.output:
        print("gaps found - review the report; plan-level gaps: ai-sdlc replan; execution fixes: ai-sdlc develop")
        return 1
    print("all requirements MET - next: ai-sdlc push")
    return 0


def cmd_summarize(args) -> int:
    ws = _require_workspace(args.workspace)
    config = _load_config(ws)
    markdown = generate_summary(ws)
    out = ws.state_dir / "engineering-summary.md"
    out.write_text(markdown, encoding="utf-8")
    audit = AuditLog(ws.runs_dir, time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    _commit_snapshot(
        ws,
        config,
        audit,
        [str(out.relative_to(ws.root))],
        "summary",
        "engineering summary",
    )
    print(f"engineering summary written to {out}")
    return 0


def cmd_push(args) -> int:
    ws = _require_workspace(args.workspace)
    # never ask a human to approve an impossible action: check the remote first
    if not has_remote(ws.root):
        print("error: no git remote configured - nothing to push to", file=sys.stderr)
        print("add one first: git remote add origin <repository-url>", file=sys.stderr)
        return 1
    # publishing without validation evidence is allowed but never silent
    report = ws.state_dir / "plan" / "validation-report.md"
    if not report.is_file():
        print("warning: no validation report - consider: ai-sdlc validate", file=sys.stderr)
    elif "MISSING" in report.read_text(encoding="utf-8"):
        print("warning: the validation report contains MISSING items", file=sys.stderr)
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

    sub.add_parser("test", parents=[common], help="run the project's test commands (mechanical, no LLM)")
    sub.add_parser("validate", parents=[common], help="agent checks the code against the original requirement")
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

    p_branch = sub.add_parser(
        "branch",
        parents=[common],
        help="show the recommended feature branch, or check one out and pin it",
    )
    p_branch.add_argument(
        "--suggest",
        action="store_true",
        help="report the recommendation without creating anything (default)",
    )
    p_branch.add_argument(
        "--use",
        default=None,
        help="check out this branch (creating it if needed) and pin it in the plan",
    )
    p_branch.add_argument("--json", action="store_true", help="machine-readable output")

    p_remote = sub.add_parser(
        "remote", parents=[common], help="show or set the git remote used by ai-sdlc push"
    )
    p_remote.add_argument(
        "--set", default=None, metavar="URL", help="set origin to this repository url"
    )
    p_remote.add_argument("--json", action="store_true", help="machine-readable output")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    handlers = {
        "init": cmd_init,
        "analyze": cmd_analyze,
        "plan": cmd_plan,
        "develop": cmd_run,
        "run": cmd_run,
        "test": cmd_test,
        "validate": cmd_validate,
        "status": cmd_status,
        "report": cmd_report,
        "rollback": cmd_rollback,
        "replan": cmd_replan,
        "push": cmd_push,
        "retry": cmd_retry,
        "summarize": cmd_summarize,
        "branch": cmd_branch,
        "remote": cmd_remote,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
