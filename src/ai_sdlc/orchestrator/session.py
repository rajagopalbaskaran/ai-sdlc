"""Step-wise orchestration for interactive (IDE) mode.

Same state machine as Engine.run(), turned inside out: instead of looping and
dispatching to an adapter, it answers two questions one call at a time -
"what should I do next?" (next_task) and "here is what happened" (report).
Python still owns task selection, the DAG, gates, the retry budget,
verification, commits, and the audit log. The interactive session only does
the work, where a human can watch and correct it.
"""

from __future__ import annotations

import subprocess
import time

from ai_sdlc.adapters.session import SessionAdapter
from ai_sdlc.changes import diff, snapshot
from ai_sdlc.governance.policy import check_policies
from ai_sdlc.governance.rollback import commit_task
from ai_sdlc.observability.audit import AuditLog
from ai_sdlc.orchestrator.dag import detect_cycles, eligible_tasks
from ai_sdlc.orchestrator.engine import Engine
from ai_sdlc.orchestrator.gates import GateContext, entry_gate, exit_gate
from ai_sdlc.state.plan import PlanDocument
from ai_sdlc.state.session import SessionState
from ai_sdlc.workspace import Workspace

BRIEFING_NAME = "current-task.md"


class SessionEngine(Engine):
    def __init__(self, workspace: Workspace, config: dict | None = None, audit=None):
        super().__init__(workspace, SessionAdapter(), config or {}, audit=audit)

    # --- helpers ---

    @property
    def briefing_path(self):
        return self.ws.state_dir / "plan" / BRIEFING_NAME

    def _resume_audit(self, state: SessionState) -> None:
        """Reattach to the session's run id so every process appends to one
        audit file instead of scattering a single run across dozens."""
        self.run_id = state.run_id
        self.audit = AuditLog(self.ws.runs_dir, state.run_id)

    def _retry_budget(self) -> int:
        return int(self.config.get("retry_budget", 2))

    @staticmethod
    def _no_session(task_id: str = "") -> dict:
        return {
            "task": task_id,
            "status": "error",
            "verified": False,
            "verify_command": None,
            "files_changed": [],
            "committed": False,
            "attempts": 0,
            "retries_left": 0,
            "reason": "no active session - run: ai-sdlc session start",
        }

    # --- lifecycle ---

    def start(self, retry_blocked: bool = False) -> dict:
        doc = PlanDocument.load(self.ws.plan_path)
        detect_cycles(doc.tasks)

        # crash recovery: work in flight when a previous session stopped
        for task in doc.tasks:
            if task.status == "in_progress":
                self._set_status(doc, task.id, "pending")
                self.audit.event(
                    "decision",
                    subject="crash_recovery",
                    choice=f"reset {task.id} to pending",
                    reasons=["task was in flight when a previous session stopped"],
                )

        approvals = self._load_approvals()

        stored_sha, current_sha = approvals.get("analysis_sha"), self.ws.analysis_sha()
        if approvals.get("plan") and stored_sha and current_sha and stored_sha != current_sha:
            reason = (
                "stale analysis: requirement-analysis.md changed since plan approval "
                "- run: ai-sdlc replan"
            )
            self.audit.event("gate", stage="develop", kind="entry", passed=False, reasons=[reason])
            return {
                "ok": False,
                "run_id": self.run_id,
                "branch": None,
                "reasons": [reason],
                "blocked": [],
            }

        gate = entry_gate("develop", GateContext(tasks=doc.tasks, approvals=approvals))
        self.audit.event(
            "gate", stage="develop", kind="entry", passed=gate.passed, reasons=gate.reasons
        )
        if not gate.passed:
            # no stdin prompting here: the session asks the human in chat and
            # records the answer with 'ai-sdlc approve'
            return {
                "ok": False,
                "run_id": self.run_id,
                "branch": None,
                "reasons": gate.reasons,
                "blocked": [],
            }

        branch = doc.meta.get("branch")
        if (self.ws.root / ".git").is_dir() and not branch:
            return {
                "ok": False,
                "run_id": self.run_id,
                "branch": None,
                "reasons": ["no branch pinned in the plan - run: ai-sdlc branch --use <name>"],
                "blocked": [],
            }

        blocked = self.blocked_report(doc)
        if retry_blocked:
            for task_id, _ in blocked:
                self._set_status(doc, task_id, "pending")
                self.audit.event(
                    "decision",
                    subject="task_retry",
                    choice=f"reset {task_id} to pending",
                    reasons=["human confirmed the blocking cause was addressed"],
                )
            blocked = []

        state = SessionState(run_id=self.run_id, started_at=time.time(), branch=branch)
        state.save(self.ws)
        self.audit.event("run_started", mode="session")
        return {
            "ok": True,
            "run_id": self.run_id,
            "branch": branch,
            "reasons": [],
            "blocked": [list(pair) for pair in blocked],
        }

    def next_task(self) -> dict:
        state = SessionState.load(self.ws)
        if state is None:
            return {
                "done": True,
                "task": None,
                "reason": "no active session - run: ai-sdlc session start",
                "briefing_path": None,
            }
        self._resume_audit(state)
        doc = PlanDocument.load(self.ws.plan_path)

        if state.active_task:
            return {
                "done": False,
                "task": None,
                "reason": (
                    f"task {state.active_task} is still in flight - report it first: "
                    f"ai-sdlc report-task --task {state.active_task} --result pass|fail"
                ),
                "briefing_path": str(self.briefing_path),
            }

        batch = eligible_tasks(doc.tasks)
        if not batch:
            remaining = [t for t in doc.tasks if t.status not in ("completed", "rolled_back")]
            reason = (
                "all tasks are in a terminal state"
                if not remaining
                else "no eligible task: "
                + ", ".join(f"{t.id}={t.status}" for t in remaining)
            )
            return {"done": True, "task": None, "reason": reason, "briefing_path": None}

        task = batch[0]
        attempt = state.attempts.get(task.id, 0) + 1
        budget = self._retry_budget()
        self._set_status(doc, task.id, "in_progress")
        self.audit.event("task_started", task=task.id, persona=task.persona, title=task.title)

        state.active_task = task.id
        state.snapshot = {p: list(sig) for p, sig in snapshot(self.ws.root).items()}
        state.save(self.ws)

        self.briefing_path.write_text(self._briefing(task, attempt, state), encoding="utf-8")
        return {
            "done": False,
            "task": {
                "id": task.id,
                "title": task.title,
                "persona": task.persona,
                "attempt": attempt,
                "retries_left": max(0, budget - (attempt - 1)),
                "last_error": state.last_error.get(task.id, ""),
            },
            "reason": "",
            "briefing_path": str(self.briefing_path),
        }

    def _briefing(self, task, attempt: int, state: SessionState) -> str:
        """Everything a stateless persona agent needs, in one file. Returned
        as a path rather than JSON because the knowledge base is far too large
        to sit in a command's stdout."""
        parts = [
            f"# Task {task.id}: {task.title}",
            "",
            f"- persona: {task.persona}",
            f"- attempt: {attempt} of {self._retry_budget() + 1}",
            f"- branch: {state.branch or '(no git repository)'}",
            "",
            "## Instructions",
            "",
            task.body or "(no task body in the plan)",
        ]
        previous = state.last_error.get(task.id)
        if previous:
            parts += ["", "## Previous attempt failed", "", previous]
        parts += ["", "## Agent context", "", self._context_for(task)]
        return "\n".join(parts) + "\n"

    # --- reporting ---

    def _verify(self, task) -> tuple[bool, str | None, str]:
        """Re-derive pass/fail from an exit code. A session's own claim is
        never sufficient evidence: it reports on its own work. Returns
        (verified, command, detail); verified is False when nothing ran."""
        command = self.config.get("task_verify_command")
        if not command:
            return False, None, "no task_verify_command configured"
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=self.ws.root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.config.get("test_timeout_seconds", 1800),
            )
            ok = proc.returncode == 0
            detail = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()[-2000:]
        except subprocess.TimeoutExpired:
            return False, command, "verification command timed out"
        return ok, command, detail

    def report(self, task_id: str, claimed_ok: bool, error: str | None = None) -> dict:
        state = SessionState.load(self.ws)
        if state is None:
            return self._no_session(task_id)
        self._resume_audit(state)
        doc = PlanDocument.load(self.ws.plan_path)
        try:
            task = doc.get(task_id)
        except KeyError as exc:
            result = self._no_session(task_id)
            result["reason"] = str(exc)
            return result

        before = {p: tuple(sig) for p, sig in state.snapshot.items()}
        files_changed = diff(before, snapshot(self.ws.root))

        ok, reason = claimed_ok, error or ""
        verified, command, detail = False, None, ""
        if ok:
            violations = check_policies(
                files_changed, self.ws.root, self.config.get("diff_limit", 500)
            )
            if violations:
                ok, reason = False, "policy: " + "; ".join(violations)
            else:
                verified, command, detail = self._verify(task)
                if command and not verified:
                    ok, reason = False, f"verification command failed: {detail[-500:]}"

        attempts = state.attempts.get(task_id, 0) + 1
        state.attempts[task_id] = attempts
        state.active_task = None
        budget = self._retry_budget()
        committed = False

        if ok:
            self._set_status(doc, task_id, "completed")
            self.audit.event(
                "task_completed",
                task=task_id,
                files_changed=len(files_changed),
                verified=verified,
                verify_command=command,
            )
            if (
                files_changed
                and self.config.get("commit_mode", "auto") != "off"
                and (self.ws.root / ".git").is_dir()
            ):
                committed = commit_task(self.ws.root, task.id, task.title, paths=files_changed)
                self.audit.event("commit", task=task_id, committed=committed)
            state.last_error.pop(task_id, None)
            status, retries_left = "completed", max(0, budget - (attempts - 1))
        else:
            state.last_error[task_id] = reason or "unspecified failure"
            self.audit.event(
                "task_failed", task=task_id, error=state.last_error[task_id], attempt=attempts
            )
            if attempts > budget:
                self._set_status(doc, task_id, "blocked")
                status, retries_left = "blocked", 0
            else:
                self._set_status(doc, task_id, "pending")
                self.audit.event("retry", task=task_id, attempt=attempts)
                status, retries_left = "pending", budget - attempts + 1

        state.save(self.ws)
        return {
            "task": task_id,
            "status": status,
            "verified": verified,
            "verify_command": command,
            "files_changed": files_changed,
            "committed": committed,
            "attempts": attempts,
            "retries_left": retries_left,
            "reason": reason,
        }

    def end(self) -> dict:
        state = SessionState.load(self.ws)
        if state is not None:
            self._resume_audit(state)
        doc = PlanDocument.load(self.ws.plan_path)
        gate = exit_gate("develop", GateContext(tasks=doc.tasks, approvals=self._load_approvals()))
        self.audit.event(
            "gate", stage="develop", kind="exit", passed=gate.passed, reasons=gate.reasons
        )
        status = "completed" if gate.passed else "halted"
        self._commit_run_record(status)
        self.audit.event("run_completed")
        SessionState.clear(self.ws)
        return {
            "status": status,
            "completed": sum(1 for t in doc.tasks if t.status == "completed"),
            "blocked": sum(1 for t in doc.tasks if t.status == "blocked"),
            "reasons": gate.reasons,
        }
