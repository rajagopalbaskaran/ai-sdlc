"""The orchestrator engine: pick -> dispatch -> validate -> update -> repeat.

Reads the implementation plan (the execution state), dispatches eligible
tasks to persona agents through an adapter, enforces gates, approvals,
retries, and policies, and records everything in the audit log. Safe to
interrupt at any point: state on disk stays consistent and a later run
resumes where this one stopped.
"""

from __future__ import annotations

import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

import json

from ai_sdlc.adapters.base import Adapter, AdapterResult
from ai_sdlc.changes import diff, snapshot
from ai_sdlc.governance.approvals import request_approval
from ai_sdlc.governance.branching import checkout_branch, has_remote, push_branch
from ai_sdlc.governance.policy import check_policies
from ai_sdlc.governance.retry import RetryPolicy
from ai_sdlc.governance.rollback import (
    commit_paths,
    commit_task,
    dirty_app_paths,
    paths_dirty,
)
from ai_sdlc.observability.audit import AuditLog
from ai_sdlc.orchestrator.dag import detect_cycles, eligible_tasks, terminal
from ai_sdlc.orchestrator.gates import GateContext, entry_gate, exit_gate
from ai_sdlc.state.kb import load_kb
from ai_sdlc.state.plan import PlanDocument, Task
from ai_sdlc.state.profile import load_profile
from ai_sdlc.workspace import Workspace


_BUG_WORDS = re.compile(r"\b(bug|fix|defect|hotfix)\b", re.IGNORECASE)
_TITLE_PREFIX = re.compile(
    r"(?i)^(requirement analysis|requirement|bug report|bug|fix|enhancement)\s*:\s*"
)


def recommend_branch(ws: Workspace) -> tuple[str, str]:
    """Recommend a branch named after the requirement being executed:
    feature/<subject> for new work, fix/<subject> for bug fixes. Falls back
    to the workspace name when no analysis exists. Returns (name, source)."""
    title = ""
    if ws.analysis_path.is_file():
        for line in ws.analysis_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
    kind = "fix" if title and _BUG_WORDS.search(title) else "feature"
    subject = _TITLE_PREFIX.sub("", title) if title else ""
    slug = re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")
    if len(slug) > 40:
        slug = slug[:40].rstrip("-")
        # never cut mid-word: drop the trailing fragment
        if "-" in slug:
            slug = slug.rsplit("-", 1)[0]
    if not slug:
        return f"feature/{ws.root.name}", "default from workspace name"
    return f"{kind}/{slug}", "derived from the requirement analysis title"


@dataclass
class RunSummary:
    status: str  # completed | stopped | halted
    completed: int = 0
    blocked: int = 0
    rolled_back: int = 0


class Engine:
    def __init__(
        self,
        workspace: Workspace,
        adapter: Adapter,
        config: dict | None = None,
        audit: AuditLog | None = None,
        input_fn: Callable[[str], str] = input,
        echo: bool = False,
    ):
        self.ws = workspace
        self.adapter = adapter
        self.config = config or {}
        self.run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.audit = audit or AuditLog(workspace.runs_dir, self.run_id, echo=echo)
        self.input_fn = input_fn
        self._lock = threading.Lock()

    # --- approvals (persisted so a granted approval survives restarts) ---

    @property
    def _approvals_path(self) -> Path:
        return self.ws.state_dir / "approvals.yaml"

    def _load_approvals(self) -> dict:
        if self._approvals_path.is_file():
            return yaml.safe_load(self._approvals_path.read_text(encoding="utf-8")) or {}
        return {}

    def _save_approvals(self, approvals: dict) -> None:
        self._approvals_path.write_text(yaml.safe_dump(approvals), encoding="utf-8")

    def _ensure_approval(self, gate: str, prompt: str, approvals: dict) -> bool:
        if approvals.get(gate):
            return True
        decision = request_approval(prompt, input_fn=self.input_fn)
        self.audit.event("approval", gate=gate, decision=decision)
        if decision == "modify":
            if gate == "plan":
                print(
                    "to modify: edit .ai-sdlc/plan/implementation-plan.md "
                    "(prose and task bodies), then rerun: ai-sdlc develop"
                )
            else:
                print(f"gate '{gate}' not approved; adjust, then rerun: ai-sdlc develop")
        if decision == "approve":
            approvals[gate] = True
            if gate == "plan":
                # fingerprint the analysis this plan approval was based on,
                # so a later analysis change is detected as staleness
                sha = self.ws.analysis_sha()
                if sha:
                    approvals["analysis_sha"] = sha
            self._save_approvals(approvals)
            return True
        return False

    # --- context assembly: stateless agents get everything from here ---

    def _context_for(self, task: Task) -> str:
        parts: list[str] = []
        persona_file = self.ws.state_dir / "personas" / f"{task.persona}.md"
        if persona_file.is_file():
            parts.append(persona_file.read_text(encoding="utf-8"))
        profile = load_profile(self.ws.state_dir)
        if profile:
            parts.append("## Project profile\n\n" + profile)
        for name, content in load_kb(self.ws.state_dir).items():
            parts.append(f"## Knowledge base: {name}\n\n{content}")
        return "\n\n".join(parts)

    # --- task execution ---

    def _set_status(self, doc: PlanDocument, task_id: str, status: str) -> None:
        with self._lock:
            doc.set_status(task_id, status)
            doc.save()

    def _execute_task(self, doc: PlanDocument, task: Task) -> None:
        self._set_status(doc, task.id, "in_progress")
        self.audit.event("task_started", task=task.id, persona=task.persona, title=task.title)
        started_at = time.time()
        context = self._context_for(task)
        retry = RetryPolicy(self.config.get("retry_budget", 2))
        before = snapshot(self.ws.root)

        def attempt(last_error: str | None) -> AdapterResult:
            """One full cycle: do the work, then validate it. A validation
            failure is a failed attempt whose reason feeds the next try -
            the agent gets the same self-correction chance for policy
            violations as for crashes."""
            ctx = context
            if last_error:
                ctx = context + f"\n\n## Previous attempt failed\n\n{last_error}"
            result = self.adapter.execute(task.persona, ctx, task)
            if not result.ok:
                return result
            files_changed = sorted(
                set(result.files_changed) | set(diff(before, snapshot(self.ws.root)))
            )
            violations = check_policies(
                files_changed, self.ws.root, self.config.get("diff_limit", 500)
            )
            if violations:
                return AdapterResult(
                    ok=False,
                    output=result.output,
                    files_changed=files_changed,
                    error="policy: " + "; ".join(violations),
                )
            return AdapterResult(ok=True, output=result.output, files_changed=files_changed)

        result = retry.attempt(
            task, attempt, on_retry=lambda **kw: self.audit.event("retry", **kw)
        )

        elapsed = round(time.time() - started_at)
        if not result.ok:
            self.audit.event(
                "task_failed", task=task.id, error=result.error or "unknown", seconds=elapsed
            )
            self._set_status(doc, task.id, "blocked")
            return

        self._set_status(doc, task.id, "completed")
        self.audit.event(
            "task_completed", task=task.id, files_changed=len(result.files_changed), seconds=elapsed
        )
        self._maybe_commit(task, result.files_changed)

    def _maybe_commit(self, task: Task, files_changed: list[str]) -> None:
        """Local per-task save-point in the TARGET workspace. Never pushes.
        Stages exactly the task's own files so leftovers cannot be absorbed.
        commit_mode: auto (default) | ask (human per commit) | off."""
        mode = self.config.get("commit_mode", "auto")
        if mode == "off" or not files_changed:
            return
        if not (self.ws.root / ".git").is_dir():
            return
        if mode == "ask":
            try:
                decision = request_approval(
                    f"Commit changes for task {task.id}?", input_fn=self.input_fn
                )
            except KeyboardInterrupt:
                # interrupt at the prompt = skip this commit, then safe-stop
                self.audit.event("approval", gate=f"commit:{task.id}", decision="interrupted")
                raise
            self.audit.event("approval", gate=f"commit:{task.id}", decision=decision)
            if decision != "approve":
                return
        with self._lock:
            committed = commit_task(self.ws.root, task.id, task.title, paths=files_changed)
        self.audit.event("commit", task=task.id, committed=committed)

    def _recover_leftovers(self) -> None:
        """Uncommitted application changes at run start (e.g. a task whose
        commit prompt was interrupted last run) get their own attributed
        commit instead of silently riding along with the next task."""
        mode = self.config.get("commit_mode", "auto")
        if mode == "off" or not (self.ws.root / ".git").is_dir():
            return
        leftovers = dirty_app_paths(self.ws.root)
        if not leftovers:
            return
        if mode == "ask":
            print(f"{len(leftovers)} uncommitted file(s) from a previous run: {', '.join(leftovers[:5])}")
            decision = request_approval("Commit them as recovered work?", input_fn=self.input_fn)
            self.audit.event("approval", gate="recovered_commit", decision=decision)
            if decision != "approve":
                return
        committed = commit_paths(
            self.ws.root, leftovers, "recovered",
            "uncommitted changes from a previous interrupted run",
        )
        self.audit.event("commit", task="recovered", committed=committed, files=leftovers)

    # --- blocked-task guidance ---

    @staticmethod
    def _interactive() -> bool:
        try:
            return bool(sys.stdin and sys.stdin.isatty())
        except (AttributeError, ValueError):
            return False

    def blocked_report(self, doc: PlanDocument | None = None) -> list[tuple[str, str]]:
        """Blocked task ids with their most recent failure reason from the
        audit history - so humans see WHY, not just WHAT."""
        doc = doc or PlanDocument.load(self.ws.plan_path)
        reasons = {t.id: "" for t in doc.tasks if t.status == "blocked"}
        if not reasons:
            return []
        for log in sorted(self.ws.runs_dir.glob("audit-*.jsonl")):
            for line in log.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                if event.get("type") == "task_failed" and event.get("task") in reasons:
                    reasons[event["task"]] = event.get("error", "")
        return sorted(reasons.items())

    def _offer_blocked_retry(self, doc: PlanDocument, retry_blocked: bool) -> None:
        blocked = self.blocked_report(doc)
        if not blocked:
            return
        if not retry_blocked and self._interactive():
            print(f"{len(blocked)} blocked task(s):")
            for task_id, reason in blocked:
                print(f"  {task_id} - {reason or 'see audit log'}")
            try:
                answer = self.input_fn(
                    "Have the causes been addressed? Retry blocked tasks now? [y/N]: "
                )
            except (EOFError, OSError):
                answer = ""
            retry_blocked = answer.strip().lower() in ("y", "yes")
        if retry_blocked:
            for task_id, _ in blocked:
                self._set_status(doc, task_id, "pending")
                self.audit.event(
                    "decision",
                    subject="task_retry",
                    choice=f"reset {task_id} to pending",
                    reasons=["human confirmed the blocking cause was addressed"],
                )

    def _commit_run_record(self, status: str) -> None:
        """Version the execution record at the run boundary: audit log, plan
        statuses, approvals. Task commits deliberately exclude .ai-sdlc so
        rollbacks cannot corrupt state; run boundaries are where the state is
        consistent and worth freezing."""
        if not (self.ws.root / ".git").is_dir():
            return
        mode = self.config.get("commit_mode", "auto")
        if mode == "off":
            return
        paths: list[str] = []
        for candidate in (
            ".ai-sdlc/runs",
            ".ai-sdlc/plan/implementation-plan.md",
            ".ai-sdlc/approvals.yaml",
        ):
            path = self.ws.root / candidate
            if path.is_file() or (path.is_dir() and any(f.is_file() for f in path.rglob("*"))):
                paths.append(candidate)
        if not paths or not paths_dirty(self.ws.root, paths):
            return
        if mode == "ask":
            try:
                decision = request_approval(
                    f"Commit the run record ({self.run_id})?", input_fn=self.input_fn
                )
            except KeyboardInterrupt:
                # never crash on an interrupt during shutdown bookkeeping
                self.audit.event("approval", gate="run_record", decision="interrupted")
                return
            self.audit.event("approval", gate="run_record", decision=decision)
            if decision != "approve":
                return
        committed = commit_paths(
            self.ws.root, paths, "run", f"run {self.run_id} record ({status})"
        )
        self.audit.event("commit", task="run-record", committed=committed)

    # --- the run loop ---

    def run(self, parallel: bool | None = None, retry_blocked: bool = False) -> RunSummary:
        if parallel is None:
            parallel = bool(self.config.get("parallel", False))

        doc = PlanDocument.load(self.ws.plan_path)
        detect_cycles(doc.tasks)

        # crash recovery: work that was in flight when a previous run stopped
        # is returned to pending so it re-executes
        for task in doc.tasks:
            if task.status == "in_progress":
                self._set_status(doc, task.id, "pending")
                self.audit.event(
                    "decision",
                    subject="crash_recovery",
                    choice=f"reset {task.id} to pending",
                    reasons=["task was in flight when a previous run stopped"],
                )

        approvals = self._load_approvals()
        self.audit.event("run_started", parallel=parallel)

        # stale-analysis gate: upstream output changed after plan approval ->
        # refuse to execute a plan derived from an outdated analysis
        stored_sha = approvals.get("analysis_sha")
        current_sha = self.ws.analysis_sha()
        if approvals.get("plan") and stored_sha and current_sha and stored_sha != current_sha:
            reason = "stale analysis: requirement-analysis.md changed since plan approval - run: ai-sdlc replan"
            self.audit.event("gate", stage="develop", kind="entry", passed=False, reasons=[reason])
            self.audit.event(
                "decision",
                subject="stale_analysis",
                choice="halt run",
                reasons=["analysis fingerprint no longer matches the approved plan"],
            )
            self.audit.event("run_stopped", reason="stale analysis")
            print(reason)
            self._commit_run_record("halted")
            return self._summary(doc, "halted")

        gate = entry_gate("develop", GateContext(tasks=doc.tasks, approvals=approvals))
        if not gate.passed and any("approved" in r for r in gate.reasons):
            if self._ensure_approval("plan", "Approve the implementation plan?", approvals):
                gate = entry_gate("develop", GateContext(tasks=doc.tasks, approvals=approvals))
        self.audit.event(
            "gate", stage="develop", kind="entry", passed=gate.passed, reasons=gate.reasons
        )
        if not gate.passed:
            self.audit.event("run_stopped", reason="entry gate failed")
            self._commit_run_record("halted")
            return self._summary(doc, "halted")

        # blocked work never resumes silently: offer the human the decision
        # (or honor an explicit --retry-blocked)
        self._offer_blocked_retry(doc, retry_blocked)

        # a run with no runnable work must say so, not vacuously succeed
        # (and must not create branches or ask vacuous questions); blocked
        # tasks are NOT "no work" - they surface via the halted path below
        if not any(t.status in ("pending", "in_progress", "blocked") for t in doc.tasks):
            print("nothing to execute: all tasks are already in a terminal state")
            print("if you analyzed a new requirement, create its tasks first: ai-sdlc plan")
            self.audit.event(
                "decision",
                subject="no_work",
                choice="nothing to execute",
                reasons=["no pending tasks in the plan"],
            )
            self.audit.event("run_completed")
            self._commit_run_record("no-work")
            return self._summary(doc, "completed")

        # feature-branch lifecycle: agents work on a branch recorded in the
        # plan; main stays clean. No-op when the workspace has no git repo.
        branch: str | None = None
        if (self.ws.root / ".git").is_dir():
            if doc.meta.get("branch"):
                branch, source = doc.meta["branch"], "recorded in the plan"
            elif self.config.get("branch"):
                branch, source = self.config["branch"], "set in config.yaml"
            else:
                branch, source = recommend_branch(self.ws)
                # interactive runs get a say: recommend the default, accept a
                # custom name; unattended runs take the default silently
                try:
                    interactive = bool(sys.stdin and sys.stdin.isatty())
                except (AttributeError, ValueError):
                    interactive = False
                if interactive:
                    try:
                        answer = self.input_fn(
                            f"Branch for this work [Enter = {branch}]: "
                        ).strip()
                    except (EOFError, OSError):
                        answer = ""
                    if answer:
                        branch, source = answer, "chosen by human"
                    else:
                        source = "recommended default accepted by human"
            if doc.meta.get("branch") != branch:
                with self._lock:
                    doc.set_meta(branch=branch)
                    doc.save()
            self.audit.event(
                "decision", subject="branch_selection", choice=branch, reasons=[source]
            )
            switched = checkout_branch(self.ws.root, branch)
            self.audit.event("branch", name=branch, ok=switched)
            self._recover_leftovers()

        self.audit.event("stage_started", stage="develop")
        try:
            while not terminal(doc.tasks):
                batch = eligible_tasks(doc.tasks)
                if not batch:
                    break
                if parallel and len(batch) > 1:
                    # independent tasks run concurrently; this pool join is
                    # the synchronization barrier before the next wave
                    with ThreadPoolExecutor(max_workers=min(4, len(batch))) as pool:
                        list(pool.map(lambda t: self._execute_task(doc, t), batch))
                else:
                    self._execute_task(doc, batch[0])
        except KeyboardInterrupt:
            with self._lock:
                doc.save()
            self.audit.event("run_stopped", reason="safe-stop (interrupt)")
            self._commit_run_record("stopped")
            return self._summary(doc, "stopped")

        self.audit.event("stage_completed", stage="develop")
        exit_result = exit_gate("develop", GateContext(tasks=doc.tasks, approvals=approvals))
        self.audit.event(
            "gate",
            stage="develop",
            kind="exit",
            passed=exit_result.passed,
            reasons=exit_result.reasons,
        )
        if exit_result.passed and "deploy_ready" in self.config.get("approval_gates", []):
            self._ensure_approval(
                "deploy_ready", "Mark this workspace deploy-ready?", approvals
            )
        # freeze the execution record before offering to publish, so a
        # pushed branch carries its own audit trail
        final_status = "completed" if exit_result.passed else "halted"
        self._commit_run_record(final_status)
        # push gate: publishing is high-impact -> asked EVERY time, never
        # persisted, never automatic
        if exit_result.passed and branch and has_remote(self.ws.root):
            decision = request_approval(
                f"Push branch {branch} to origin?", input_fn=self.input_fn
            )
            self.audit.event("approval", gate="push", decision=decision)
            if decision == "approve":
                pushed, detail = push_branch(self.ws.root, branch)
                self.audit.event("push", branch=branch, ok=pushed, detail=detail)
        self.audit.event("run_completed")
        return self._summary(doc, final_status)

    def _summary(self, doc: PlanDocument, status: str) -> RunSummary:
        return RunSummary(
            status=status,
            completed=sum(1 for t in doc.tasks if t.status == "completed"),
            blocked=sum(1 for t in doc.tasks if t.status == "blocked"),
            rolled_back=sum(1 for t in doc.tasks if t.status == "rolled_back"),
        )
