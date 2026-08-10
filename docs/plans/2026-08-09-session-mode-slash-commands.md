# Session Mode and Slash Commands Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let every ai-sdlc stage run as a Claude Code slash command (`/ai-sdlc init|analyze|plan|develop|test|validate|push|...`) inside the IDE, while Python keeps sole ownership of the state machine, gates, retries, commits, and the audit log.

**Architecture:** Today the Python engine drives and calls Claude Code headless through an adapter. Session mode inverts only the *execution* half: a new step-wise CLI surface (`session start`, `next`, `report`, `session end`) hands the interactive Claude Code session one task at a time and takes back a pass/fail report. Python still picks the task, enforces the DAG and gates, counts retries, verifies the result independently, commits, and writes the audit trail. The session never writes plan yaml and never calls raw git. Because each CLI call is a separate process, session identity (run id, snapshot, retry counters) is persisted in `.ai-sdlc/session.yaml`.

**Tech Stack:** Python 3.11+, argparse, pyyaml, pytest 8, git, Claude Code slash commands (markdown files in `.claude/commands/`).

## Global Constraints

- Python `>=3.11` (per `pyproject.toml`); no new runtime dependencies beyond `pyyaml>=6.0`.
- **ASCII only** in every file created or modified - no Unicode arrows, em-dashes, or box drawing.
- All new work lands on branch `v1` of the `ai-sdlc` repository.
- The interactive session MUST NOT run raw `git` mutations, edit `.ai-sdlc/plan/implementation-plan.md` yaml, or write `.ai-sdlc/approvals.yaml`. Every such change goes through the CLI so it is audited.
- `ai-sdlc report --result pass` is a claim, not evidence. Python re-derives pass/fail from a verify command exit code where one is configured, and records `verified: false` in the audit when none is.
- Approval defaults stay fail-closed: EOF or absent human input is never `approve` (existing `request_approval` contract at `src/ai_sdlc/governance/approvals.py:25-27`).
- Existing headless mode (`ai-sdlc develop` with the `claude-code` adapter) must keep passing its current tests unchanged. Session mode is additive.
- Line style: follow the surrounding code - `from __future__ import annotations`, dataclasses, module docstrings explaining *why*.

## File Structure

**Create:**
- `src/ai_sdlc/state/session.py` - `SessionState` persisted at `.ai-sdlc/session.yaml`: run id, branch, active task, per-task attempt counters, last error, pre-task file snapshot.
- `src/ai_sdlc/adapters/session.py` - `SessionAdapter`, an `Adapter` that refuses to execute; it exists so `Engine` construction works in session mode where the interactive session is the agent.
- `src/ai_sdlc/orchestrator/session.py` - `SessionEngine(Engine)` with `start()`, `next_task()`, `report()`, `end()`. Reuses `Engine._context_for`, `_set_status`, `_load_approvals`, `_commit_run_record`, `blocked_report`.
- `src/ai_sdlc/cli_session.py` - argparse handlers for the new commands, kept out of the already-large `cli.py`.
- `src/ai_sdlc/templates/commands/ai-sdlc.md` - the slash command file installed into a target project's `.claude/commands/`.
- `tests/test_session_state.py`, `tests/test_session_engine.py`, `tests/test_cli_session.py`, `tests/test_commands_install.py`.

**Modify:**
- `src/ai_sdlc/workspace.py` - add `session_path`, `commands_dir`, `install_commands()`.
- `src/ai_sdlc/governance/branching.py` - add `set_remote()`, `remote_url()`.
- `src/ai_sdlc/orchestrator/engine.py:424-459` - extract branch selection into `Engine.select_branch()` so session mode reuses it, and stop recording a non-interactive default as if a human accepted it.
- `src/ai_sdlc/adapters/base.py:31-44` - register the `session` adapter name.
- `src/ai_sdlc/cli.py` - register new subparsers, add `--json` to `status`, `test`, `validate`, `push`.
- `src/ai_sdlc/templates/config.yaml` - document `task_verify_command`.
- `pyproject.toml` - ship `templates/commands/*.md` as package data.
- `README.md` - document slash-command usage.

---

### Task 1: Session state persistence

**Files:**
- Create: `src/ai_sdlc/state/session.py`
- Modify: `src/ai_sdlc/workspace.py`
- Test: `tests/test_session_state.py`

**Interfaces:**
- Consumes: `Workspace` from `ai_sdlc.workspace`.
- Produces: `SessionState` dataclass with fields `run_id: str`, `started_at: float`, `branch: str | None`, `active_task: str | None`, `attempts: dict[str, int]`, `last_error: dict[str, str]`, `snapshot: dict[str, list[int]]`; classmethods `load(ws) -> SessionState | None`, methods `save(ws) -> None`, classmethod `clear(ws) -> bool`. `Workspace.session_path -> Path`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_session_state.py
import time

from ai_sdlc.state.session import SessionState
from ai_sdlc.workspace import Workspace


def test_session_roundtrip_and_clear(tmp_workspace):
    Workspace.init(tmp_workspace)
    ws = Workspace(tmp_workspace)

    assert SessionState.load(ws) is None

    state = SessionState(run_id="20260809-101010-abc123", started_at=time.time())
    state.branch = "feature/demo"
    state.attempts["T1"] = 2
    state.last_error["T1"] = "compile failed"
    state.snapshot = {"a.py": [1, 2]}
    state.save(ws)

    assert ws.session_path.is_file()
    loaded = SessionState.load(ws)
    assert loaded.run_id == "20260809-101010-abc123"
    assert loaded.branch == "feature/demo"
    assert loaded.attempts["T1"] == 2
    assert loaded.last_error["T1"] == "compile failed"
    assert loaded.snapshot == {"a.py": [1, 2]}

    assert SessionState.clear(ws) is True
    assert SessionState.load(ws) is None
    assert SessionState.clear(ws) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_session_state.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ai_sdlc.state.session'`

- [ ] **Step 3: Write the implementation**

```python
# src/ai_sdlc/state/session.py
"""Session state: identity for an interactive (IDE-driven) run.

Headless runs hold run identity in one Engine object for the whole run.
Session mode spans many separate CLI processes - `ai-sdlc next` and
`ai-sdlc report` are different invocations - so run id, retry counters, the
pre-task file snapshot, and the pinned branch must live on disk between
calls. This file is orchestrator-owned state; the interactive session never
writes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path

import yaml


@dataclass
class SessionState:
    run_id: str
    started_at: float
    branch: str | None = None
    active_task: str | None = None
    attempts: dict[str, int] = field(default_factory=dict)
    last_error: dict[str, str] = field(default_factory=dict)
    snapshot: dict[str, list[int]] = field(default_factory=dict)

    @classmethod
    def load(cls, ws) -> "SessionState | None":
        path = ws.session_path
        if not path.is_file():
            return None
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict) or "run_id" not in data:
            return None
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, ws) -> None:
        ws.session_path.parent.mkdir(parents=True, exist_ok=True)
        ws.session_path.write_text(
            yaml.safe_dump(asdict(self), sort_keys=False), encoding="utf-8"
        )

    @classmethod
    def clear(cls, ws) -> bool:
        """Remove the session file. Returns False when there was none."""
        path: Path = ws.session_path
        if not path.is_file():
            return False
        path.unlink()
        return True
```

Add to `src/ai_sdlc/workspace.py`, after the `analysis_path` property:

```python
    @property
    def session_path(self) -> Path:
        return self.state_dir / "session.yaml"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_session_state.py -v`
Expected: PASS (3 assertions groups, 1 test)

- [ ] **Step 5: Commit**

```bash
git add src/ai_sdlc/state/session.py src/ai_sdlc/workspace.py tests/test_session_state.py
git commit -m "feat: persist session state across CLI invocations"
```

---

### Task 2: Session adapter

**Files:**
- Create: `src/ai_sdlc/adapters/session.py`
- Modify: `src/ai_sdlc/adapters/base.py:31-44`
- Test: `tests/test_adapters.py` (append)

**Interfaces:**
- Consumes: `Adapter`, `AdapterResult` from `ai_sdlc.adapters.base`.
- Produces: `SessionAdapter` with `name = "session"`; `execute()` raises `RuntimeError`. `build_adapter("session", config)` returns it.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_adapters.py
def test_session_adapter_refuses_to_execute():
    import pytest

    from ai_sdlc.adapters.base import build_adapter
    from ai_sdlc.state.plan import Task

    adapter = build_adapter("session", {})
    assert adapter.name == "session"
    task = Task(id="T1", title="One", status="pending")
    with pytest.raises(RuntimeError, match="ai-sdlc next"):
        adapter.execute("developer", "context", task)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_adapters.py::test_session_adapter_refuses_to_execute -v`
Expected: FAIL with `ValueError: unknown adapter 'session'`

- [ ] **Step 3: Write the implementation**

```python
# src/ai_sdlc/adapters/session.py
"""Adapter for interactive (IDE) mode.

In session mode the agent is the interactive Claude Code session driving the
CLI, not a subprocess the engine spawns. This adapter exists so Engine can be
constructed with the same contract; calling execute() means orchestration
took the headless path by mistake, which is a bug worth failing loudly on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ai_sdlc.adapters.base import Adapter, AdapterResult

if TYPE_CHECKING:
    from ai_sdlc.state.plan import Task


class SessionAdapter(Adapter):
    name = "session"

    def execute(self, persona: str, context: str, task: "Task") -> AdapterResult:
        raise RuntimeError(
            "session adapter cannot execute tasks: the interactive session performs "
            "the work - drive it with 'ai-sdlc next' and 'ai-sdlc report'"
        )
```

In `src/ai_sdlc/adapters/base.py`, inside `build_adapter`, add the import and branch:

```python
    from ai_sdlc.adapters.session import SessionAdapter
    ...
    if name == "session":
        return SessionAdapter()
```

and update the error message to `"unknown adapter {name!r}; available: mock, claude-code, session"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_adapters.py -v`
Expected: PASS, all pre-existing adapter tests still pass

- [ ] **Step 5: Commit**

```bash
git add src/ai_sdlc/adapters/session.py src/ai_sdlc/adapters/base.py tests/test_adapters.py
git commit -m "feat: add session adapter for interactive mode"
```

---

### Task 3: Explicit branch selection (suggest / use) and the isatty honesty fix

**Files:**
- Modify: `src/ai_sdlc/orchestrator/engine.py:424-459`
- Create: `src/ai_sdlc/cli_session.py`
- Modify: `src/ai_sdlc/cli.py` (parser + handler map)
- Test: `tests/test_cli_session.py`

**Interfaces:**
- Consumes: `recommend_branch(ws) -> tuple[str, str]` at `engine.py:53`, `checkout_branch(root, name) -> bool`, `current_branch(root) -> str | None`.
- Produces: `cmd_branch(args) -> int` in `ai_sdlc.cli_session`; CLI `ai-sdlc branch [--suggest] [--use NAME] [--json]`. JSON shape: `{"recommended": str, "source": str, "pinned": str | None, "current": str | None, "created": bool}`.

**Why the engine change:** at `engine.py:436-450` a non-tty run silently accepts the recommended branch. Under a slash command stdin is never a tty, so the human confirmation the README promises stops happening without a trace. Session mode requires the branch to be pinned explicitly beforehand.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_session.py
import json
import subprocess

from ai_sdlc.cli import main
from ai_sdlc.state.plan import PlanDocument
from ai_sdlc.workspace import Workspace

PLAN = """# Implementation Plan

### Task 1: One

```yaml
id: T1
status: pending
depends_on: []
persona: developer
```

Do the thing.
"""


def _git_repo(root):
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=root, check=True)


def _seed(root):
    main(["init", "--workspace", str(root)])
    state = root / ".ai-sdlc"
    (state / "plan" / "implementation-plan.md").write_text(PLAN, encoding="utf-8")
    (state / "approvals.yaml").write_text("plan: true\n", encoding="utf-8")
    return state


def test_branch_suggest_does_not_create(tmp_workspace, capsys):
    _git_repo(tmp_workspace)
    _seed(tmp_workspace)

    rc = main(["branch", "--suggest", "--json", "--workspace", str(tmp_workspace)])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["recommended"].startswith("feature/")
    assert data["pinned"] is None
    assert data["created"] is False

    branches = subprocess.run(
        ["git", "branch", "--format=%(refname:short)"],
        cwd=tmp_workspace, capture_output=True, text=True, check=True,
    ).stdout.split()
    assert data["recommended"] not in branches


def test_branch_use_creates_and_pins(tmp_workspace, capsys):
    _git_repo(tmp_workspace)
    _seed(tmp_workspace)

    rc = main(["branch", "--use", "fix/demo", "--json", "--workspace", str(tmp_workspace)])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["pinned"] == "fix/demo"
    assert data["current"] == "fix/demo"
    assert data["created"] is True

    doc = PlanDocument.load(Workspace(tmp_workspace).plan_path)
    assert doc.meta["branch"] == "fix/demo"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli_session.py -v`
Expected: FAIL with `argparse` error - `invalid choice: 'branch'`

- [ ] **Step 3: Write the implementation**

Create `src/ai_sdlc/cli_session.py`:

```python
"""CLI surface for interactive (IDE / slash-command) mode.

These commands expose the orchestrator's decisions one at a time so an
interactive Claude Code session can perform the work while Python keeps the
state machine: it picks the task, counts retries, verifies the result,
commits, and writes the audit log. Kept separate from cli.py, which already
carries the headless surface.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

from ai_sdlc.governance.branching import checkout_branch, current_branch
from ai_sdlc.observability.audit import AuditLog
from ai_sdlc.orchestrator.engine import recommend_branch
from ai_sdlc.state.plan import PlanDocument
from ai_sdlc.workspace import Workspace


def _require_workspace(root: Path) -> Workspace:
    ws = Workspace(root)
    if not ws.exists():
        raise SystemExit(f"error: {ws.state_dir} not found; run: ai-sdlc init --workspace {root}")
    return ws


def _new_audit(ws: Workspace) -> AuditLog:
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    return AuditLog(ws.runs_dir, run_id)


def _emit(payload: dict, as_json: bool, lines: list[str]) -> None:
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        for line in lines:
            print(line)


def cmd_branch(args) -> int:
    ws = _require_workspace(args.workspace)
    doc = PlanDocument.load(ws.plan_path)
    recommended, source = recommend_branch(ws)
    payload = {
        "recommended": recommended,
        "source": source,
        "pinned": doc.meta.get("branch"),
        "current": current_branch(ws.root),
        "created": False,
    }

    if not args.use:
        _emit(
            payload,
            args.json,
            [
                f"recommended: {recommended}  ({source})",
                f"pinned in plan: {payload['pinned'] or '(none)'}",
                f"currently checked out: {payload['current'] or '(none)'}",
                "",
                f"to use it: ai-sdlc branch --use {recommended}",
            ],
        )
        return 0

    if not (ws.root / ".git").is_dir():
        print("error: workspace is not a git repository", file=sys.stderr)
        return 1

    audit = _new_audit(ws)
    reason = "chosen by human" if args.use != recommended else "recommended default accepted by human"
    ok = checkout_branch(ws.root, args.use)
    audit.event("decision", subject="branch_selection", choice=args.use, reasons=[reason])
    audit.event("branch", name=args.use, ok=ok)
    if not ok:
        print(f"error: could not check out branch {args.use}", file=__import__("sys").stderr)
        return 1
    doc.set_meta(branch=args.use)
    doc.save()
    payload.update({"pinned": args.use, "current": current_branch(ws.root), "created": True})
    _emit(payload, args.json, [f"branch {args.use} checked out and pinned in the plan"])
    return 0
```

In `src/ai_sdlc/cli.py`, add the import near the other local imports:

```python
from ai_sdlc.cli_session import cmd_branch
```

In `_build_parser()`, before the `return parser` line:

```python
    p_branch = sub.add_parser(
        "branch", parents=[common], help="show the recommended feature branch, or check one out and pin it"
    )
    p_branch.add_argument("--suggest", action="store_true", help="report the recommendation without creating anything (default)")
    p_branch.add_argument("--use", default=None, help="check out this branch (creating it if needed) and pin it in the plan")
    p_branch.add_argument("--json", action="store_true", help="machine-readable output")
```

In `main()`'s `handlers` dict add `"branch": cmd_branch,`.

Now the engine honesty fix. In `src/ai_sdlc/orchestrator/engine.py`, replace the `else:` arm of the branch selection block (currently lines 432-450) with:

```python
            else:
                branch, source = recommend_branch(self.ws)
                # interactive runs get a say; non-interactive runs take the
                # default but must not record it as a human decision
                if self._interactive():
                    try:
                        answer = self.input_fn(f"Branch for this work [Enter = {branch}]: ").strip()
                    except (EOFError, OSError):
                        answer = ""
                    if answer:
                        branch, source = answer, "chosen by human"
                    else:
                        source = "recommended default accepted by human"
                else:
                    source = source + " (accepted automatically: no interactive stdin)"
```

This reuses the existing `Engine._interactive()` static method at `engine.py:263-268`, so the local `try/except` around `sys.stdin.isatty()` at lines 436-439 is deleted.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cli_session.py -v && python -m pytest tests/test_engine.py -v`
Expected: PASS for both, including all pre-existing engine tests

- [ ] **Step 5: Commit**

```bash
git add src/ai_sdlc/cli_session.py src/ai_sdlc/cli.py src/ai_sdlc/orchestrator/engine.py tests/test_cli_session.py
git commit -m "feat: add 'ai-sdlc branch' and stop recording auto-picked branches as human choices"
```

---

### Task 4: Remote configuration

**Files:**
- Modify: `src/ai_sdlc/governance/branching.py`
- Modify: `src/ai_sdlc/cli_session.py`, `src/ai_sdlc/cli.py`
- Test: `tests/test_branching.py` (append), `tests/test_cli_session.py` (append)

**Interfaces:**
- Consumes: `_git(root, *args)` helper at `branching.py:13`.
- Produces: `remote_url(root, name="origin") -> str | None`, `set_remote(root, url, name="origin") -> bool` (adds when absent, updates when present). `cmd_remote(args) -> int`; CLI `ai-sdlc remote [--set URL] [--json]`. JSON shape: `{"origin": str | None, "changed": bool}`.

**Why:** every project the framework generates has no git remote (verified across url-shortener, address-book, quick-poll, voting-service, signet, prime-printer), so `has_remote()` is False, the engine's push gate at `engine.py:501` is skipped silently, and `ai-sdlc push` exits 1. Nothing in the framework can wire an origin.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_branching.py
def test_set_remote_adds_then_updates(tmp_path):
    import subprocess

    from ai_sdlc.governance.branching import has_remote, remote_url, set_remote

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)

    assert remote_url(root) is None
    assert has_remote(root) is False

    assert set_remote(root, "https://example.com/a.git") is True
    assert remote_url(root) == "https://example.com/a.git"
    assert has_remote(root) is True

    assert set_remote(root, "https://example.com/b.git") is True
    assert remote_url(root) == "https://example.com/b.git"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_branching.py -v`
Expected: FAIL with `ImportError: cannot import name 'remote_url'`

- [ ] **Step 3: Write the implementation**

Append to `src/ai_sdlc/governance/branching.py`:

```python
def remote_url(root: Path, name: str = "origin") -> str | None:
    result = _git(root, "remote", "get-url", name)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def set_remote(root: Path, url: str, name: str = "origin") -> bool:
    """Point `name` at `url`, adding the remote when it does not exist yet."""
    if remote_url(root, name) is None:
        return _git(root, "remote", "add", name, url).returncode == 0
    return _git(root, "remote", "set-url", name, url).returncode == 0
```

Append to `src/ai_sdlc/cli_session.py`:

```python
def cmd_remote(args) -> int:
    from ai_sdlc.governance.branching import remote_url, set_remote

    ws = _require_workspace(args.workspace)
    if not (ws.root / ".git").is_dir():
        print("error: workspace is not a git repository", file=sys.stderr)
        return 1
    if not args.set:
        url = remote_url(ws.root)
        _emit(
            {"origin": url, "changed": False},
            args.json,
            [
                f"origin: {url}" if url else "origin: (none configured)",
                "" if url else "set one with: ai-sdlc remote --set <repository-url>",
            ],
        )
        return 0
    audit = _new_audit(ws)
    ok = set_remote(ws.root, args.set)
    audit.event("decision", subject="remote_set", choice=args.set, reasons=["human supplied the remote url"])
    audit.event("remote", url=args.set, ok=ok)
    if not ok:
        print(f"error: could not set origin to {args.set}", file=sys.stderr)
        return 1
    _emit({"origin": args.set, "changed": True}, args.json, [f"origin set to {args.set}"])
    return 0
```

In `cli.py`, import `cmd_remote`, add to the handler map as `"remote"`, and register:

```python
    p_remote = sub.add_parser("remote", parents=[common], help="show or set the git remote used by ai-sdlc push")
    p_remote.add_argument("--set", default=None, metavar="URL", help="set origin to this repository url")
    p_remote.add_argument("--json", action="store_true", help="machine-readable output")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_branching.py tests/test_cli_session.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ai_sdlc/governance/branching.py src/ai_sdlc/cli_session.py src/ai_sdlc/cli.py tests/test_branching.py
git commit -m "feat: add 'ai-sdlc remote' so generated projects can be wired to github"
```

---

### Task 5: Non-stdin approvals

**Files:**
- Modify: `src/ai_sdlc/cli_session.py`, `src/ai_sdlc/cli.py`
- Test: `tests/test_cli_session.py` (append)

**Interfaces:**
- Produces: `cmd_approve(args) -> int`; CLI `ai-sdlc approve --gate {plan,deploy_ready} [--revoke] [--json]`. JSON shape: `{"gate": str, "approved": bool, "analysis_sha": str | None}`.

**Why:** `request_approval` returns `"reject"` on EOF, and under the Bash tool stdin is never a tty, so every gate auto-rejects forever. The human approves in the IDE chat; the session then records that decision through this command. `approve` writes the same `analysis_sha` fingerprint that `Engine._ensure_approval` does at `engine.py:129-137`, so the stale-analysis gate at `engine.py:376-390` keeps working identically.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_cli_session.py
def test_approve_plan_records_analysis_fingerprint(tmp_workspace, capsys):
    import yaml

    state = _seed(tmp_workspace)
    (state / "approvals.yaml").write_text("plan: false\n", encoding="utf-8")
    (state / "plan" / "requirement-analysis.md").write_text("# Analysis\n", encoding="utf-8")

    rc = main(["approve", "--gate", "plan", "--json", "--workspace", str(tmp_workspace)])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["approved"] is True
    assert data["analysis_sha"]

    stored = yaml.safe_load((state / "approvals.yaml").read_text(encoding="utf-8"))
    assert stored["plan"] is True
    assert stored["analysis_sha"] == data["analysis_sha"]


def test_approve_revoke_clears_plan(tmp_workspace, capsys):
    import yaml

    state = _seed(tmp_workspace)
    main(["approve", "--gate", "plan", "--workspace", str(tmp_workspace)])
    capsys.readouterr()

    rc = main(["approve", "--gate", "plan", "--revoke", "--json", "--workspace", str(tmp_workspace)])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["approved"] is False
    stored = yaml.safe_load((state / "approvals.yaml").read_text(encoding="utf-8"))
    assert stored["plan"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli_session.py -k approve -v`
Expected: FAIL with `argparse` error - `invalid choice: 'approve'`

- [ ] **Step 3: Write the implementation**

Append to `src/ai_sdlc/cli_session.py`:

```python
def cmd_approve(args) -> int:
    """Record a human approval made in the IDE chat rather than at a stdin
    prompt. The decision still comes from a human; only the channel differs,
    and the audit event says so."""
    import yaml

    ws = _require_workspace(args.workspace)
    path = ws.state_dir / "approvals.yaml"
    approvals = {}
    if path.is_file():
        approvals = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    audit = _new_audit(ws)
    if args.revoke:
        approvals[args.gate] = False
        if args.gate == "plan":
            approvals.pop("analysis_sha", None)
            approvals.pop("deploy_ready", None)
        sha = None
    else:
        approvals[args.gate] = True
        sha = ws.analysis_sha() if args.gate == "plan" else None
        if sha:
            approvals["analysis_sha"] = sha
    path.write_text(yaml.safe_dump(approvals), encoding="utf-8")
    audit.event(
        "approval",
        gate=args.gate,
        decision="reject" if args.revoke else "approve",
        channel="ide_session",
    )
    _emit(
        {"gate": args.gate, "approved": not args.revoke, "analysis_sha": sha},
        args.json,
        [f"gate '{args.gate}' {'revoked' if args.revoke else 'approved'}"],
    )
    return 0
```

In `cli.py`, import `cmd_approve`, add `"approve"` to the handler map, and register:

```python
    p_approve = sub.add_parser(
        "approve", parents=[common], help="record a human approval taken in the IDE chat (no stdin prompt)"
    )
    p_approve.add_argument("--gate", required=True, choices=["plan", "deploy_ready"], help="gate being decided")
    p_approve.add_argument("--revoke", action="store_true", help="withdraw the approval instead of granting it")
    p_approve.add_argument("--json", action="store_true", help="machine-readable output")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cli_session.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ai_sdlc/cli_session.py src/ai_sdlc/cli.py tests/test_cli_session.py
git commit -m "feat: add 'ai-sdlc approve' so gates work without an interactive stdin"
```

---

### Task 6: SessionEngine - start and next_task

**Files:**
- Create: `src/ai_sdlc/orchestrator/session.py`
- Test: `tests/test_session_engine.py`

**Interfaces:**
- Consumes: `Engine` (`_context_for`, `_set_status`, `_load_approvals`, `_interactive`, `blocked_report`), `detect_cycles`, `eligible_tasks`, `entry_gate`, `GateContext`, `SessionState`, `snapshot`.
- Produces:
  - `SessionEngine(ws, config=None, audit=None)`.
  - `start(retry_blocked: bool = False) -> dict` returning `{"ok": bool, "run_id": str, "branch": str | None, "reasons": list[str], "blocked": list[list[str]]}`.
  - `next_task() -> dict` returning `{"done": bool, "task": dict | None, "reason": str, "briefing_path": str | None}` where `task` is `{"id","title","persona","attempt","retries_left","last_error"}`.
  - Briefing written to `.ai-sdlc/plan/current-task.md`.

**Design note:** the persona context plus the whole knowledge base is far too large to return as a JSON field. `next_task()` writes a briefing markdown file and returns its path; the slash command reads that file.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_session_engine.py
from ai_sdlc.orchestrator.session import SessionEngine
from ai_sdlc.state.session import SessionState
from ai_sdlc.workspace import Workspace

PLAN = """# Implementation Plan

### Task 1: First

```yaml
id: T1
status: pending
depends_on: []
persona: developer
```

Build the first thing.

### Task 2: Second

```yaml
id: T2
status: pending
depends_on: [T1]
persona: developer
```

Build the second thing.
"""


def _ws(root, approved=True):
    Workspace.init(root)
    ws = Workspace(root)
    ws.plan_path.write_text(PLAN, encoding="utf-8")
    (ws.state_dir / "approvals.yaml").write_text(
        f"plan: {'true' if approved else 'false'}\n", encoding="utf-8"
    )
    return ws


def test_start_refuses_without_plan_approval(tmp_workspace):
    ws = _ws(tmp_workspace, approved=False)
    result = SessionEngine(ws).start()
    assert result["ok"] is False
    assert any("approved" in r for r in result["reasons"])
    assert SessionState.load(ws) is None


def test_start_then_next_returns_first_eligible_task(tmp_workspace):
    ws = _ws(tmp_workspace)
    engine = SessionEngine(ws)
    started = engine.start()
    assert started["ok"] is True
    assert SessionState.load(ws).run_id == started["run_id"]

    nxt = SessionEngine(ws).next_task()
    assert nxt["done"] is False
    assert nxt["task"]["id"] == "T1"
    assert nxt["task"]["attempt"] == 1
    assert nxt["task"]["retries_left"] == 2
    briefing = ws.state_dir / "plan" / "current-task.md"
    assert briefing.is_file()
    assert "Build the first thing." in briefing.read_text(encoding="utf-8")
    # dependency not satisfied yet, so T2 is not offered
    assert "T2" not in nxt["task"]["id"]
    # the task is marked in flight, and the pre-task snapshot is recorded
    state = SessionState.load(ws)
    assert state.active_task == "T1"


def test_next_without_session_is_an_error(tmp_workspace):
    ws = _ws(tmp_workspace)
    result = SessionEngine(ws).next_task()
    assert result["done"] is True
    assert "session" in result["reason"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_session_engine.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ai_sdlc.orchestrator.session'`

- [ ] **Step 3: Write the implementation**

```python
# src/ai_sdlc/orchestrator/session.py
"""Step-wise orchestration for interactive (IDE) mode.

Same state machine as Engine.run(), turned inside out: instead of looping and
dispatching to an adapter, it answers two questions one call at a time -
"what should I do next?" (next_task) and "here is what happened" (report).
Python still owns task selection, the DAG, gates, retry budget, verification,
commits, and the audit log. The interactive session only does the work.
"""

from __future__ import annotations

import time
import uuid

from ai_sdlc.adapters.session import SessionAdapter
from ai_sdlc.changes import snapshot
from ai_sdlc.observability.audit import AuditLog
from ai_sdlc.orchestrator.dag import detect_cycles, eligible_tasks
from ai_sdlc.orchestrator.gates import GateContext, entry_gate
from ai_sdlc.state.plan import PlanDocument
from ai_sdlc.state.session import SessionState
from ai_sdlc.orchestrator.engine import Engine
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
        audit file instead of scattering a run across dozens."""
        self.run_id = state.run_id
        self.audit = AuditLog(self.ws.runs_dir, state.run_id)

    def _retry_budget(self) -> int:
        return int(self.config.get("retry_budget", 2))

    # --- lifecycle ---

    def start(self, retry_blocked: bool = False) -> dict:
        doc = PlanDocument.load(self.ws.plan_path)
        detect_cycles(doc.tasks)

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
            reason = "stale analysis: requirement-analysis.md changed since plan approval - run: ai-sdlc replan"
            self.audit.event("gate", stage="develop", kind="entry", passed=False, reasons=[reason])
            return {"ok": False, "run_id": self.run_id, "branch": None, "reasons": [reason], "blocked": []}

        gate = entry_gate("develop", GateContext(tasks=doc.tasks, approvals=approvals))
        self.audit.event("gate", stage="develop", kind="entry", passed=gate.passed, reasons=gate.reasons)
        if not gate.passed:
            # no stdin prompting here: the session asks the human in chat and
            # records the answer with 'ai-sdlc approve'
            return {"ok": False, "run_id": self.run_id, "branch": None, "reasons": gate.reasons, "blocked": []}

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
                "reason": f"task {state.active_task} is still in flight - report it first: ai-sdlc report --task {state.active_task} --result pass|fail",
                "briefing_path": str(self.briefing_path),
            }

        batch = eligible_tasks(doc.tasks)
        if not batch:
            remaining = [t for t in doc.tasks if t.status not in ("completed", "rolled_back")]
            reason = "all tasks are in a terminal state" if not remaining else (
                "no eligible task: " + ", ".join(f"{t.id}={t.status}" for t in remaining)
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_session_engine.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/ai_sdlc/orchestrator/session.py tests/test_session_engine.py
git commit -m "feat: SessionEngine start/next_task for step-wise orchestration"
```

---

### Task 7: SessionEngine.report with independent verification

**Files:**
- Modify: `src/ai_sdlc/orchestrator/session.py`
- Modify: `src/ai_sdlc/templates/config.yaml`
- Test: `tests/test_session_engine.py` (append)

**Interfaces:**
- Consumes: `check_policies(files_changed, root, diff_limit)`, `commit_task(root, task_id, title, paths)`, `diff(before, after)`, `SessionState`.
- Produces: `SessionEngine.report(task_id: str, claimed_ok: bool, error: str | None = None) -> dict` returning `{"task": str, "status": str, "verified": bool, "verify_command": str | None, "files_changed": list[str], "committed": bool, "attempts": int, "retries_left": int, "reason": str}`.
- Produces: `SessionEngine.end() -> dict` returning `{"status": str, "completed": int, "blocked": int, "reasons": list[str]}`.
- New config key `task_verify_command` (string, optional).

**The guardrail:** a `--result pass` claim is downgraded to failure when the verify command exits non-zero, and downgraded to `verified: false` in the audit when no verify command is configured. A claim never overrides an exit code.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_session_engine.py
import subprocess


def _git_repo(root):
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=root, check=True)


def test_report_pass_is_rejected_when_verify_command_fails(tmp_workspace):
    ws = _ws(tmp_workspace)
    config = {"task_verify_command": "exit 3", "retry_budget": 1}
    SessionEngine(ws, config).start()
    SessionEngine(ws, config).next_task()

    result = SessionEngine(ws, config).report("T1", claimed_ok=True)
    assert result["verified"] is False
    assert result["status"] == "pending"
    assert "verification" in result["reason"]
    assert result["retries_left"] == 1

    from ai_sdlc.state.plan import PlanDocument
    assert PlanDocument.load(ws.plan_path).get("T1").status == "pending"


def test_report_pass_with_passing_verify_completes_and_commits(tmp_workspace):
    _git_repo(tmp_workspace)
    ws = _ws(tmp_workspace)
    config = {"task_verify_command": "exit 0", "commit_mode": "auto"}
    SessionEngine(ws, config).start()
    SessionEngine(ws, config).next_task()
    (tmp_workspace / "app.py").write_text("print('hi')\n", encoding="utf-8")

    result = SessionEngine(ws, config).report("T1", claimed_ok=True)
    assert result["verified"] is True
    assert result["status"] == "completed"
    assert any(p.endswith("app.py") for p in result["files_changed"])
    assert result["committed"] is True

    log = subprocess.run(
        ["git", "log", "--oneline", "-1"], cwd=tmp_workspace,
        capture_output=True, text=True, check=True,
    ).stdout
    assert "T1" in log


def test_report_exhausting_retry_budget_blocks_the_task(tmp_workspace):
    ws = _ws(tmp_workspace)
    config = {"retry_budget": 1}

    SessionEngine(ws, config).start()
    SessionEngine(ws, config).next_task()
    first = SessionEngine(ws, config).report("T1", claimed_ok=False, error="compile error")
    assert first["status"] == "pending"
    assert first["retries_left"] == 0

    SessionEngine(ws, config).next_task()
    second = SessionEngine(ws, config).report("T1", claimed_ok=False, error="compile error again")
    assert second["status"] == "blocked"

    from ai_sdlc.state.plan import PlanDocument
    assert PlanDocument.load(ws.plan_path).get("T1").status == "blocked"


def test_report_without_verify_command_records_unverified(tmp_workspace):
    ws = _ws(tmp_workspace)
    SessionEngine(ws, {}).start()
    SessionEngine(ws, {}).next_task()
    result = SessionEngine(ws, {}).report("T1", claimed_ok=True)
    assert result["status"] == "completed"
    assert result["verified"] is False
    assert result["verify_command"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_session_engine.py -k report -v`
Expected: FAIL with `AttributeError: 'SessionEngine' object has no attribute 'report'`

- [ ] **Step 3: Write the implementation**

Add these imports at the top of `src/ai_sdlc/orchestrator/session.py`:

```python
import subprocess

from ai_sdlc.changes import diff
from ai_sdlc.governance.policy import check_policies
from ai_sdlc.governance.rollback import commit_task
from ai_sdlc.orchestrator.gates import exit_gate
```

Append these methods to `SessionEngine`:

```python
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
            return {"task": task_id, "status": "error", "verified": False, "verify_command": None,
                    "files_changed": [], "committed": False, "attempts": 0, "retries_left": 0,
                    "reason": "no active session - run: ai-sdlc session start"}
        self._resume_audit(state)
        doc = PlanDocument.load(self.ws.plan_path)
        try:
            task = doc.get(task_id)
        except KeyError as exc:
            return {"task": task_id, "status": "error", "verified": False, "verify_command": None,
                    "files_changed": [], "committed": False, "attempts": 0, "retries_left": 0,
                    "reason": str(exc)}

        before = {p: tuple(sig) for p, sig in state.snapshot.items()}
        files_changed = diff(before, snapshot(self.ws.root))

        ok, reason = claimed_ok, error or ""
        verified, command, detail = (False, None, "")
        if ok:
            violations = check_policies(files_changed, self.ws.root, self.config.get("diff_limit", 500))
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
                "task_completed", task=task_id, files_changed=len(files_changed),
                verified=verified, verify_command=command,
            )
            if files_changed and self.config.get("commit_mode", "auto") != "off" \
                    and (self.ws.root / ".git").is_dir():
                committed = commit_task(self.ws.root, task.id, task.title, paths=files_changed)
                self.audit.event("commit", task=task_id, committed=committed)
            state.last_error.pop(task_id, None)
            status, retries_left = "completed", max(0, budget - (attempts - 1))
        else:
            state.last_error[task_id] = reason or "unspecified failure"
            self.audit.event("task_failed", task=task_id, error=state.last_error[task_id], attempt=attempts)
            if attempts > budget:
                self._set_status(doc, task_id, "blocked")
                status, retries_left = "blocked", 0
            else:
                self._set_status(doc, task_id, "pending")
                self.audit.event("retry", task=task_id, attempt=attempts)
                status, retries_left = "pending", budget - attempts + 1

        state.save(self.ws)
        return {
            "task": task_id, "status": status, "verified": verified, "verify_command": command,
            "files_changed": files_changed, "committed": committed, "attempts": attempts,
            "retries_left": retries_left, "reason": reason,
        }

    def end(self) -> dict:
        state = SessionState.load(self.ws)
        if state is not None:
            self._resume_audit(state)
        doc = PlanDocument.load(self.ws.plan_path)
        gate = exit_gate("develop", GateContext(tasks=doc.tasks, approvals=self._load_approvals()))
        self.audit.event("gate", stage="develop", kind="exit", passed=gate.passed, reasons=gate.reasons)
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
```

Add to `src/ai_sdlc/templates/config.yaml`, after `task_timeout_seconds`:

```yaml
task_verify_command: ""    # per-task proof, re-run by ai-sdlc report; a session's
                           # own "it passed" claim is never trusted on its own, e.g.
                           #   task_verify_command: "cd backend && mvn -q compile"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_session_engine.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/ai_sdlc/orchestrator/session.py src/ai_sdlc/templates/config.yaml tests/test_session_engine.py
git commit -m "feat: SessionEngine.report verifies independently of the session's claim"
```

---

### Task 8: CLI wiring for session, next, report

**Files:**
- Modify: `src/ai_sdlc/cli_session.py`, `src/ai_sdlc/cli.py`
- Test: `tests/test_cli_session.py` (append)

**Interfaces:**
- Produces: `cmd_session(args) -> int`, `cmd_next(args) -> int`, `cmd_report(args) -> int` (named `cmd_task_report` in code to avoid colliding with the existing `cmd_report` audit-rendering command in `cli.py`).
- CLI: `ai-sdlc session {start,status,end} [--retry-blocked] [--json]`, `ai-sdlc next [--json]`, `ai-sdlc report-task --task ID --result {pass,fail} [--error TEXT] [--json]`.

**Naming note:** `ai-sdlc report` already exists and renders the audit log to markdown. The new command is `report-task` so the existing one is not broken.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_cli_session.py
def test_session_start_next_report_end_cycle(tmp_workspace, capsys):
    _git_repo(tmp_workspace)
    state = _seed(tmp_workspace)
    (state / "config.yaml").write_text("adapter: session\ncommit_mode: auto\n", encoding="utf-8")
    main(["branch", "--use", "feature/demo", "--workspace", str(tmp_workspace)])
    capsys.readouterr()

    assert main(["session", "start", "--json", "--workspace", str(tmp_workspace)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    assert main(["next", "--json", "--workspace", str(tmp_workspace)]) == 0
    nxt = json.loads(capsys.readouterr().out)
    assert nxt["task"]["id"] == "T1"

    (tmp_workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
    assert main([
        "report-task", "--task", "T1", "--result", "pass",
        "--json", "--workspace", str(tmp_workspace),
    ]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["status"] == "completed"

    assert main(["next", "--json", "--workspace", str(tmp_workspace)]) == 0
    assert json.loads(capsys.readouterr().out)["done"] is True

    assert main(["session", "end", "--json", "--workspace", str(tmp_workspace)]) == 0
    end = json.loads(capsys.readouterr().out)
    assert end["status"] == "completed"
    assert not (state / "session.yaml").exists()


def test_session_start_without_pinned_branch_fails(tmp_workspace, capsys):
    _git_repo(tmp_workspace)
    _seed(tmp_workspace)
    rc = main(["session", "start", "--json", "--workspace", str(tmp_workspace)])
    assert rc == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert any("branch" in r for r in data["reasons"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli_session.py -k session -v`
Expected: FAIL with `argparse` error - `invalid choice: 'session'`

- [ ] **Step 3: Write the implementation**

Append to `src/ai_sdlc/cli_session.py`:

```python
def _session_engine(ws: Workspace):
    from ai_sdlc.cli import _load_config
    from ai_sdlc.orchestrator.session import SessionEngine

    return SessionEngine(ws, _load_config(ws))


def cmd_session(args) -> int:
    from ai_sdlc.state.session import SessionState

    ws = _require_workspace(args.workspace)
    engine = _session_engine(ws)

    if args.action == "start":
        result = engine.start(retry_blocked=args.retry_blocked)
        lines = (
            [f"session {result['run_id']} started on branch {result['branch'] or '(no git)'}",
             "next: ai-sdlc next"]
            if result["ok"]
            else ["cannot start:"] + [f"  - {r}" for r in result["reasons"]]
        )
        _emit(result, args.json, lines)
        return 0 if result["ok"] else 1

    if args.action == "status":
        state = SessionState.load(ws)
        payload = {
            "active": state is not None,
            "run_id": state.run_id if state else None,
            "branch": state.branch if state else None,
            "active_task": state.active_task if state else None,
            "attempts": state.attempts if state else {},
        }
        _emit(payload, args.json, [
            f"session {payload['run_id']}" if state else "no active session",
            f"active task: {payload['active_task'] or '(none)'}" if state else "start one: ai-sdlc session start",
        ])
        return 0

    result = engine.end()
    _emit(result, args.json, [
        f"session ended: {result['status']} "
        f"(completed={result['completed']} blocked={result['blocked']})"
    ] + [f"  - {r}" for r in result["reasons"]])
    return 0 if result["status"] == "completed" else 1


def cmd_next(args) -> int:
    ws = _require_workspace(args.workspace)
    result = _session_engine(ws).next_task()
    if result["done"] or result["task"] is None:
        _emit(result, args.json, [result["reason"] or "nothing to do"])
        return 0
    task = result["task"]
    _emit(result, args.json, [
        f"{task['id']}: {task['title']}",
        f"  persona={task['persona']} attempt={task['attempt']} retries_left={task['retries_left']}",
        f"  briefing: {result['briefing_path']}",
        "",
        "do the work, then: ai-sdlc report-task --task "
        f"{task['id']} --result pass|fail",
    ])
    return 0


def cmd_task_report(args) -> int:
    ws = _require_workspace(args.workspace)
    result = _session_engine(ws).report(
        args.task, claimed_ok=(args.result == "pass"), error=args.error
    )
    _emit(result, args.json, [
        f"{result['task']}: {result['status']} "
        f"(verified={result['verified']} files={len(result['files_changed'])} "
        f"committed={result['committed']} retries_left={result['retries_left']})",
    ] + ([f"  reason: {result['reason']}"] if result["reason"] else []))
    return 0 if result["status"] in ("completed", "pending") else 1
```

In `cli.py`, extend the import to `from ai_sdlc.cli_session import cmd_approve, cmd_branch, cmd_next, cmd_remote, cmd_session, cmd_task_report`, add the handler entries `"session": cmd_session, "next": cmd_next, "report-task": cmd_task_report`, and register:

```python
    p_session = sub.add_parser(
        "session", parents=[common], help="interactive (IDE) mode: start, inspect, or end a stepwise run"
    )
    p_session.add_argument("action", choices=["start", "status", "end"])
    p_session.add_argument("--retry-blocked", action="store_true", help="reset blocked tasks to pending at start")
    p_session.add_argument("--json", action="store_true", help="machine-readable output")

    p_next = sub.add_parser("next", parents=[common], help="ask the orchestrator for the next task to execute")
    p_next.add_argument("--json", action="store_true", help="machine-readable output")

    p_task_report = sub.add_parser(
        "report-task", parents=[common], help="report a task outcome; the result is independently verified"
    )
    p_task_report.add_argument("--task", required=True, help="task id being reported (e.g. T3)")
    p_task_report.add_argument("--result", required=True, choices=["pass", "fail"])
    p_task_report.add_argument("--error", default=None, help="failure detail fed back into the next attempt")
    p_task_report.add_argument("--json", action="store_true", help="machine-readable output")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cli_session.py -v`
Expected: PASS (all tests in file)

- [ ] **Step 5: Commit**

```bash
git add src/ai_sdlc/cli_session.py src/ai_sdlc/cli.py tests/test_cli_session.py
git commit -m "feat: expose session/next/report-task on the CLI"
```

---

### Task 9: JSON output for the existing stage commands

**Files:**
- Modify: `src/ai_sdlc/cli.py` (`cmd_status`, `cmd_test`, `cmd_validate`, `cmd_push`)
- Test: `tests/test_cli.py` (append)

**Interfaces:**
- Produces: `--json` on `status`, `test`, `validate`, `push`. Shapes: status `{"tasks":[{"id","status","persona","depends_on","title"}]}`; test `{"ok":bool,"commands":[{"command","ok","seconds"}]}`; validate `{"ok":bool,"report":str,"missing":int,"partial":int}`; push `{"ok":bool,"branch":str,"detail":str}`.
- Also: `push` gains `--yes` semantics unchanged, but its audit event records `channel="ide_session"` when `--yes` is used.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_cli.py
def test_status_json_lists_tasks(tmp_workspace, capsys):
    import json

    _seed(tmp_workspace)
    assert main(["status", "--json", "--workspace", str(tmp_workspace)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["tasks"][0]["id"] == "T1"
    assert data["tasks"][0]["status"] == "pending"


def test_test_json_reports_command_results(tmp_workspace, capsys):
    import json

    state = _seed(tmp_workspace)
    (state / "config.yaml").write_text(
        'adapter: mock\ntest_commands:\n  - "exit 0"\n', encoding="utf-8"
    )
    assert main(["test", "--json", "--workspace", str(tmp_workspace)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["commands"][0]["command"] == "exit 0"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli.py -k json -v`
Expected: FAIL with `argparse` error - `unrecognized arguments: --json`

- [ ] **Step 3: Write the implementation**

Add a shared helper near the top of `cli.py`:

```python
def _emit_json(payload: dict) -> None:
    print(json.dumps(payload, indent=2))
```

In `cmd_status`, replace the print loop's tail with a branch:

```python
    if getattr(args, "json", False):
        _emit_json({"tasks": [
            {"id": t.id, "status": t.status, "persona": t.persona,
             "depends_on": t.depends_on, "title": t.title}
            for t in doc.tasks
        ]})
        return 0
```

placed immediately after the `if not doc.tasks:` guard (returning `{"tasks": []}` in that case rather than the prose line when `--json` is set).

In `cmd_test`, accumulate a `results: list[dict]` alongside the existing loop (`results.append({"command": command, "ok": ok, "seconds": seconds})`) and, before the final prose returns:

```python
    if getattr(args, "json", False):
        _emit_json({"ok": all_ok, "commands": results})
        return 0 if all_ok else 1
```

In `cmd_validate`, before the trailing prose returns:

```python
    if getattr(args, "json", False):
        text = result.output
        _emit_json({
            "ok": "MISSING" not in text and "PARTIAL" not in text,
            "report": str(report),
            "missing": text.count("MISSING"),
            "partial": text.count("PARTIAL"),
        })
        return 0 if ("MISSING" not in text and "PARTIAL" not in text) else 1
```

In `cmd_push`, change the audit event to carry the channel and add the JSON branch:

```python
    audit.event("approval", gate="push", decision=decision,
                channel="ide_session" if args.yes else "stdin")
    ...
    if getattr(args, "json", False):
        _emit_json({"ok": pushed, "branch": branch, "detail": detail})
        return 0 if pushed else 1
```

Register `--json` on the four subparsers in `_build_parser()`. `p_push` already
exists at `cli.py:761`, so add one line to it:

```python
    p_push.add_argument("--json", action="store_true", help="machine-readable output")
```

The `status`, `test`, and `validate` subparsers are currently created without a
variable name at `cli.py:745-747`; replace those three bare calls with:

```python
    p_test = sub.add_parser("test", parents=[common], help="run the project's test commands (mechanical, no LLM)")
    p_test.add_argument("--json", action="store_true", help="machine-readable output")
    p_validate = sub.add_parser("validate", parents=[common], help="agent checks the code against the original requirement")
    p_validate.add_argument("--json", action="store_true", help="machine-readable output")
    p_status = sub.add_parser("status", parents=[common], help="show task states")
    p_status.add_argument("--json", action="store_true", help="machine-readable output")
```

replacing the current bare `sub.add_parser("test", ...)`, `sub.add_parser("validate", ...)`, and `sub.add_parser("status", ...)` calls at `cli.py:745-747`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cli.py -v`
Expected: PASS, including all pre-existing CLI tests

- [ ] **Step 5: Commit**

```bash
git add src/ai_sdlc/cli.py tests/test_cli.py
git commit -m "feat: add --json output to status, test, validate, and push"
```

---

### Task 10: The slash command file and its installer

**Files:**
- Create: `src/ai_sdlc/templates/commands/ai-sdlc.md`
- Modify: `src/ai_sdlc/workspace.py`, `src/ai_sdlc/cli.py`, `pyproject.toml`
- Test: `tests/test_commands_install.py`

**Interfaces:**
- Produces: `Workspace.commands_dir -> Path` (`<root>/.claude/commands`), `Workspace.install_commands(force: bool = False) -> Path | None` (returns the written path, or None when it exists and `force` is False). `cmd_install_commands(args) -> int`; CLI `ai-sdlc install-commands [--force]`. `cmd_init` calls `install_commands()` and reports it.

**Why a single file:** `/ai-sdlc init` with a space-separated subcommand requires one command file named `ai-sdlc.md` that parses `$ARGUMENTS`. Separate files per stage would produce `/ai-sdlc:init` with a colon instead.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_commands_install.py
from ai_sdlc.cli import main
from ai_sdlc.workspace import Workspace


def test_init_installs_the_slash_command(tmp_workspace):
    assert main(["init", "--workspace", str(tmp_workspace)]) == 0
    command = tmp_workspace / ".claude" / "commands" / "ai-sdlc.md"
    assert command.is_file()
    text = command.read_text(encoding="utf-8")
    assert "$ARGUMENTS" in text
    assert "ai-sdlc report-task" in text
    assert text.isascii()


def test_install_commands_is_idempotent_without_force(tmp_workspace):
    main(["init", "--workspace", str(tmp_workspace)])
    ws = Workspace(tmp_workspace)
    command = ws.commands_dir / "ai-sdlc.md"
    command.write_text("customized\n", encoding="utf-8")

    assert main(["install-commands", "--workspace", str(tmp_workspace)]) == 0
    assert command.read_text(encoding="utf-8") == "customized\n"

    assert main(["install-commands", "--force", "--workspace", str(tmp_workspace)]) == 0
    assert "$ARGUMENTS" in command.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_commands_install.py -v`
Expected: FAIL with `AssertionError` on the missing `.claude/commands/ai-sdlc.md`

- [ ] **Step 3: Write the implementation**

Create `src/ai_sdlc/templates/commands/ai-sdlc.md`:

```markdown
---
description: Run an ai-sdlc pipeline stage in this IDE session
argument-hint: init | analyze <file> | plan | develop | test | validate | status | branch | remote | push
allowed-tools: Bash(ai-sdlc:*), Read, Edit, Write, Glob, Grep
---

# ai-sdlc: $ARGUMENTS

Python owns the state machine. You execute tasks. Follow these rules exactly.

## Rules

1. NEVER run raw `git` mutations (commit, checkout, branch, push, revert). Every
   git change goes through an `ai-sdlc` command so it is audited.
2. NEVER edit `.ai-sdlc/plan/implementation-plan.md` yaml blocks or
   `.ai-sdlc/approvals.yaml`. The orchestrator is their only writer.
3. NEVER pass `--yes` or run `ai-sdlc approve` on your own initiative. Ask the
   human in chat first and act only on their answer.
4. Report honestly. `--result pass` when the work is not done is a lie the
   verification step will catch, and it wastes a retry.

## Dispatch on $ARGUMENTS

### init
Run `ai-sdlc init --workspace .`, then tell the human to fill in
`.ai-sdlc/project-profile.md` and `.ai-sdlc/knowledge-base/`.

### analyze <requirement-file>
Run `ai-sdlc analyze <requirement-file> --workspace .`. Show the human the
resulting `.ai-sdlc/plan/requirement-analysis.md` and ask them to resolve any
ambiguities before planning.

### plan
Run `ai-sdlc plan --workspace .`. Show the resulting task list
(`ai-sdlc status --json --workspace .`). Ask the human to approve the plan. If
they approve, run `ai-sdlc approve --gate plan --workspace .`.

### develop
1. `ai-sdlc branch --suggest --json --workspace .` - show the recommendation and
   ask the human which branch to use. Then
   `ai-sdlc branch --use <name> --workspace .`.
2. `ai-sdlc session start --json --workspace .`. If it reports `ok: false`, fix
   the listed reasons (usually a missing plan approval) and stop.
3. Loop:
   - `ai-sdlc next --json --workspace .`
   - If `done` is true, break.
   - Read the file at `briefing_path`. It contains the task instructions, the
     persona, the project profile, the knowledge base, and any previous failure.
   - Do the work with Edit/Write/Bash. The human can see and correct every edit.
   - `ai-sdlc report-task --task <id> --result pass|fail --error "<detail>" --json --workspace .`
   - If `status` is `pending`, the attempt failed and a retry remains: loop again.
   - If `status` is `blocked`, tell the human why and stop.
4. `ai-sdlc session end --json --workspace .`.

### test
Run `ai-sdlc test --json --workspace .`. On failure, show the failing output and
offer to fix it - but fixes belong to `develop`, not here.

### validate
Run `ai-sdlc validate --json --workspace .`. Summarize MET / PARTIAL / MISSING
counts and point the human at `.ai-sdlc/plan/validation-report.md`.

### status
Run `ai-sdlc status --json --workspace .` and render it as a short table.

### branch
Run `ai-sdlc branch --suggest --json --workspace .`, show the recommendation, ask
the human, then `ai-sdlc branch --use <name> --workspace .`.

### remote
Run `ai-sdlc remote --json --workspace .`. If no origin is configured, tell the
human to create the GitHub repository and give you its URL, then run
`ai-sdlc remote --set <url> --workspace .`.

### push
1. `ai-sdlc remote --json --workspace .` - if there is no origin, handle `remote`
   above first.
2. `ai-sdlc status --json --workspace .` and `ai-sdlc validate --json` results -
   show the human what they are about to publish.
3. Ask the human for explicit permission to push.
4. Only after they say yes: `ai-sdlc push --yes --json --workspace .`.

### anything else
Show this list of subcommands and ask what they meant.
```

Add to `src/ai_sdlc/workspace.py`:

```python
COMMANDS_DIR_NAME = ".claude/commands"
COMMAND_FILE = "ai-sdlc.md"
```

and the methods:

```python
    @property
    def commands_dir(self) -> Path:
        return self.root / ".claude" / "commands"

    def install_commands(self, force: bool = False) -> Path | None:
        """Install the /ai-sdlc slash command into the target project.
        Returns the path written, or None when a file already exists and
        force is False - a customized command is never clobbered."""
        target = self.commands_dir / COMMAND_FILE
        if target.exists() and not force:
            return None
        self.commands_dir.mkdir(parents=True, exist_ok=True)
        source = resources.files("ai_sdlc") / "templates" / "commands" / COMMAND_FILE
        shutil.copy(str(source), target)
        return target
```

In `cli.py`, extend `cmd_init` before its `return 0`:

```python
    installed = ws.install_commands()
    if installed:
        print(f"installed slash command {installed} - use /ai-sdlc in Claude Code")
```

and add a new handler:

```python
def cmd_install_commands(args) -> int:
    ws = _require_workspace(args.workspace)
    installed = ws.install_commands(force=args.force)
    if installed:
        print(f"installed {installed}")
    else:
        print(f"{ws.commands_dir / 'ai-sdlc.md'} already exists; use --force to overwrite")
    return 0
```

Register it:

```python
    p_cmds = sub.add_parser(
        "install-commands", parents=[common], help="install the /ai-sdlc slash command into .claude/commands"
    )
    p_cmds.add_argument("--force", action="store_true", help="overwrite an existing command file")
```

and add `"install-commands": cmd_install_commands` to the handler map.

In `pyproject.toml`, extend the package data:

```toml
[tool.setuptools.package-data]
ai_sdlc = ["templates/*.yaml", "templates/*.md", "templates/personas/*.md", "templates/commands/*.md"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_commands_install.py tests/test_cli.py tests/test_workspace.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ai_sdlc/templates/commands/ai-sdlc.md src/ai_sdlc/workspace.py src/ai_sdlc/cli.py pyproject.toml tests/test_commands_install.py
git commit -m "feat: ship the /ai-sdlc slash command and install it at init"
```

---

### Task 11: End-to-end session integration test

**Files:**
- Test: `tests/test_integration.py` (append)

**Interfaces:**
- Consumes: everything built in Tasks 1-10.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_integration.py
def test_full_session_pipeline_in_a_git_repo(tmp_workspace, capsys):
    import json
    import subprocess

    from ai_sdlc.cli import main
    from ai_sdlc.state.plan import PlanDocument
    from ai_sdlc.workspace import Workspace

    subprocess.run(["git", "init", "-q"], cwd=tmp_workspace, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_workspace, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_workspace, check=True)

    assert main(["init", "--workspace", str(tmp_workspace)]) == 0
    ws = Workspace(tmp_workspace)
    ws.plan_path.write_text(
        "# Implementation Plan\n\n"
        "### Task 1: One\n\n"
        "```yaml\nid: T1\nstatus: pending\ndepends_on: []\npersona: developer\n```\n\n"
        "Create app.py\n\n"
        "### Task 2: Two\n\n"
        "```yaml\nid: T2\nstatus: pending\ndepends_on: [T1]\npersona: developer\n```\n\n"
        "Create lib.py\n",
        encoding="utf-8",
    )
    (ws.state_dir / "config.yaml").write_text(
        'adapter: session\ncommit_mode: auto\ntask_verify_command: "exit 0"\n', encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=tmp_workspace, check=True)
    capsys.readouterr()

    assert main(["approve", "--gate", "plan", "--workspace", str(tmp_workspace)]) == 0
    assert main(["branch", "--use", "feature/e2e", "--workspace", str(tmp_workspace)]) == 0
    assert main(["session", "start", "--workspace", str(tmp_workspace)]) == 0
    capsys.readouterr()

    for task_id, filename in (("T1", "app.py"), ("T2", "lib.py")):
        assert main(["next", "--json", "--workspace", str(tmp_workspace)]) == 0
        nxt = json.loads(capsys.readouterr().out)
        assert nxt["task"]["id"] == task_id
        (tmp_workspace / filename).write_text("x = 1\n", encoding="utf-8")
        assert main([
            "report-task", "--task", task_id, "--result", "pass",
            "--json", "--workspace", str(tmp_workspace),
        ]) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "completed"

    assert main(["next", "--json", "--workspace", str(tmp_workspace)]) == 0
    assert json.loads(capsys.readouterr().out)["done"] is True

    assert main(["session", "end", "--json", "--workspace", str(tmp_workspace)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"

    doc = PlanDocument.load(ws.plan_path)
    assert [t.status for t in doc.tasks] == ["completed", "completed"]
    assert doc.meta["branch"] == "feature/e2e"

    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=tmp_workspace,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert branch == "feature/e2e"

    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=tmp_workspace,
        capture_output=True, text=True, check=True,
    ).stdout
    assert "T1" in log and "T2" in log

    audits = list((ws.state_dir / "runs").glob("audit-*.jsonl"))
    session_events = [
        json.loads(line)
        for path in audits
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # one session run id covers both tasks: the audit trail is not fragmented
    run_ids = {e["run_id"] for e in session_events if e["type"] in ("task_started", "task_completed")}
    assert len(run_ids) == 1
```

- [ ] **Step 2: Run test to verify it fails (or passes) honestly**

Run: `python -m pytest tests/test_integration.py -v`
Expected: PASS if Tasks 1-10 are complete. If it fails, the failure is a real integration defect - fix it before proceeding, do not weaken the assertions.

- [ ] **Step 3: Run the whole suite**

Run: `python -m pytest -q`
Expected: all tests pass, including every pre-existing test. Record the exact count in the commit message.

- [ ] **Step 4: Verify no Unicode crept in**

Run: `python -c "import pathlib,sys; bad=[str(p) for p in pathlib.Path('src').rglob('*') if p.is_file() and not p.read_bytes().decode('utf-8').isascii()]; print(bad or 'ascii clean'); sys.exit(1 if bad else 0)"`
Expected: `ascii clean`

- [ ] **Step 5: Commit**

```bash
git add tests/test_integration.py
git commit -m "test: end-to-end session pipeline with git commits and one audit run id"
```

---

### Task 12: Documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`

- [ ] **Step 1: Add a README section**

Insert after the existing "Getting Started - step by step" section a new section titled `## Two ways to run: headless or in the IDE`, covering:
- the headless path (unchanged): `ai-sdlc analyze / plan / develop / test / validate / push`
- the session path: `/ai-sdlc analyze`, `/ai-sdlc plan`, `/ai-sdlc develop`, etc., installed automatically by `ai-sdlc init` into `.claude/commands/ai-sdlc.md`
- the division of labour: Python picks tasks, counts retries, verifies, commits, audits; the session edits code in the IDE where the human can see and correct compile errors
- that both modes share one plan file and are mutually resumable
- the `task_verify_command` config key and why a session's own pass claim is not trusted
- the new commands table: `branch`, `remote`, `approve`, `session`, `next`, `report-task`, `install-commands`

- [ ] **Step 2: Add an architecture note**

In `docs/architecture.md`, add a subsection "Session mode" describing the inversion of control, the `.ai-sdlc/session.yaml` state file and why it exists (multi-process run identity), and the three invariants: the session never writes plan yaml, never runs raw git, never self-approves.

- [ ] **Step 3: Verify the docs are ASCII**

Run: `python -c "import pathlib,sys; bad=[str(p) for p in [pathlib.Path('README.md'),*pathlib.Path('docs').rglob('*.md')] if not p.read_bytes().decode('utf-8').isascii()]; print(bad or 'ascii clean'); sys.exit(1 if bad else 0)"`
Expected: `ascii clean`

- [ ] **Step 4: Commit**

```bash
git add README.md docs/architecture.md
git commit -m "docs: document session mode and the slash command surface"
```

---

## Final verification and push

- [ ] Run the full suite: `python -m pytest -q` - all green
- [ ] Confirm the branch: `git rev-parse --abbrev-ref HEAD` returns `v1`
- [ ] Ask the human for explicit push permission
- [ ] Push: `git push origin v1`

---

## Known limitations (state these; do not hide them)

1. **Verification is only as good as `task_verify_command`.** With none configured, `report-task --result pass` is taken at face value and the audit records `verified: false`. That is honest but weak; projects should configure a compile or fast-test command.
2. **Governance softens at the edges.** In headless mode the retry loop is a Python `while`; in session mode the loop lives in a markdown prompt the model follows. Python still enforces the budget and refuses out-of-order reports, but a session that simply stops calling `next` ends the run early. `ai-sdlc session status` and the exit gate make that visible rather than silent.
3. **No concurrency in session mode.** `next_task()` hands out one task at a time even when the DAG allows parallelism. Headless `--parallel` is unaffected.
4. **Approval channel changes, not the approver.** `ai-sdlc approve` records a human decision taken in chat instead of at a stdin prompt. It is guarded by prompt rules, not by code - a session that self-approves would be recorded as if a human had. The audit `channel` field at least makes the distinction auditable after the fact.
