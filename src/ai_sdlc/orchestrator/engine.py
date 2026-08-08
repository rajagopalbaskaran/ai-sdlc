"""The orchestrator engine: pick -> dispatch -> validate -> update -> repeat.

Reads the implementation plan (the execution state), dispatches eligible
tasks to persona agents through an adapter, enforces gates, approvals,
retries, and policies, and records everything in the audit log. Safe to
interrupt at any point: state on disk stays consistent and a later run
resumes where this one stopped.
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from ai_sdlc.adapters.base import Adapter
from ai_sdlc.changes import diff, snapshot
from ai_sdlc.governance.approvals import request_approval
from ai_sdlc.governance.policy import check_policies
from ai_sdlc.governance.retry import RetryPolicy
from ai_sdlc.governance.rollback import commit_task
from ai_sdlc.observability.audit import AuditLog
from ai_sdlc.orchestrator.dag import detect_cycles, eligible_tasks, terminal
from ai_sdlc.orchestrator.gates import GateContext, entry_gate, exit_gate
from ai_sdlc.state.kb import load_kb
from ai_sdlc.state.plan import PlanDocument, Task
from ai_sdlc.state.profile import load_profile
from ai_sdlc.workspace import Workspace


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
    ):
        self.ws = workspace
        self.adapter = adapter
        self.config = config or {}
        self.run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.audit = audit or AuditLog(workspace.runs_dir, self.run_id)
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
        self.audit.event("task_started", task=task.id, persona=task.persona)
        context = self._context_for(task)
        retry = RetryPolicy(self.config.get("retry_budget", 2))
        before = snapshot(self.ws.root)

        def attempt(last_error: str | None):
            ctx = context
            if last_error:
                ctx = context + f"\n\n## Previous attempt failed\n\n{last_error}"
            return self.adapter.execute(task.persona, ctx, task)

        result = retry.attempt(
            task, attempt, on_retry=lambda **kw: self.audit.event("retry", **kw)
        )

        if not result.ok:
            self.audit.event("task_failed", task=task.id, error=result.error or "unknown")
            self._set_status(doc, task.id, "blocked")
            return

        # trust the adapter's report when present, but always verify what
        # actually changed on disk
        files_changed = sorted(set(result.files_changed) | set(diff(before, snapshot(self.ws.root))))
        violations = check_policies(
            files_changed, self.ws.root, self.config.get("diff_limit", 500)
        )
        if violations:
            self.audit.event(
                "task_failed", task=task.id, error="policy: " + "; ".join(violations)
            )
            self._set_status(doc, task.id, "blocked")
            return

        self._set_status(doc, task.id, "completed")
        self.audit.event("task_completed", task=task.id, files_changed=len(files_changed))
        self._maybe_commit(task, files_changed)

    def _maybe_commit(self, task: Task, files_changed: list[str]) -> None:
        """Local per-task save-point in the TARGET workspace. Never pushes.
        commit_mode: auto (default) | ask (human per commit) | off."""
        mode = self.config.get("commit_mode", "auto")
        if mode == "off" or not files_changed:
            return
        if not (self.ws.root / ".git").is_dir():
            return
        if mode == "ask":
            decision = request_approval(
                f"Commit changes for task {task.id}?", input_fn=self.input_fn
            )
            self.audit.event("approval", gate=f"commit:{task.id}", decision=decision)
            if decision != "approve":
                return
        with self._lock:
            committed = commit_task(self.ws.root, task.id, task.title)
        self.audit.event("commit", task=task.id, committed=committed)

    # --- the run loop ---

    def run(self, parallel: bool | None = None) -> RunSummary:
        if parallel is None:
            parallel = bool(self.config.get("parallel", False))

        doc = PlanDocument.load(self.ws.plan_path)
        detect_cycles(doc.tasks)

        # crash recovery: work that was in flight when a previous run stopped
        # is returned to pending so it re-executes
        for task in doc.tasks:
            if task.status == "in_progress":
                self._set_status(doc, task.id, "pending")
                self.audit.event("decision", kind="recovered_in_flight_task", task=task.id)

        approvals = self._load_approvals()
        self.audit.event("run_started", parallel=parallel)

        # stale-analysis gate: upstream output changed after plan approval ->
        # refuse to execute a plan derived from an outdated analysis
        stored_sha = approvals.get("analysis_sha")
        current_sha = self.ws.analysis_sha()
        if approvals.get("plan") and stored_sha and current_sha and stored_sha != current_sha:
            reason = "stale analysis: requirement-analysis.md changed since plan approval - run: ai-sdlc replan"
            self.audit.event("gate", stage="develop", kind="entry", passed=False, reasons=[reason])
            self.audit.event("run_stopped", reason="stale analysis")
            print(reason)
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
            return self._summary(doc, "halted")

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
            return self._summary(doc, "stopped")

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
        self.audit.event("run_completed")
        return self._summary(doc, "completed" if exit_result.passed else "halted")

    def _summary(self, doc: PlanDocument, status: str) -> RunSummary:
        return RunSummary(
            status=status,
            completed=sum(1 for t in doc.tasks if t.status == "completed"),
            blocked=sum(1 for t in doc.tasks if t.status == "blocked"),
            rolled_back=sum(1 for t in doc.tasks if t.status == "rolled_back"),
        )
