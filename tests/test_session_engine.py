import subprocess

from ai_sdlc.orchestrator.session import SessionEngine
from ai_sdlc.state.plan import PlanDocument
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


def _git_repo(root):
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=root, check=True)


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

    state = SessionState.load(ws)
    assert state.active_task == "T1"


def test_next_without_session_is_an_error(tmp_workspace):
    ws = _ws(tmp_workspace)
    result = SessionEngine(ws).next_task()
    assert result["done"] is True
    assert "session" in result["reason"]


def test_next_refuses_a_second_task_while_one_is_in_flight(tmp_workspace):
    ws = _ws(tmp_workspace)
    SessionEngine(ws).start()
    SessionEngine(ws).next_task()

    again = SessionEngine(ws).next_task()
    assert again["task"] is None
    assert "in flight" in again["reason"]


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

    assert PlanDocument.load(ws.plan_path).get("T1").status == "pending"


def _pin_branch(ws, name="feature/test"):
    doc = PlanDocument.load(ws.plan_path)
    doc.set_meta(branch=name)
    doc.save()


def test_start_refuses_when_no_branch_is_pinned_in_a_git_repo(tmp_workspace):
    _git_repo(tmp_workspace)
    ws = _ws(tmp_workspace)
    result = SessionEngine(ws).start()
    assert result["ok"] is False
    assert any("branch" in r for r in result["reasons"])


def test_report_pass_with_passing_verify_completes_and_commits(tmp_workspace):
    _git_repo(tmp_workspace)
    ws = _ws(tmp_workspace)
    _pin_branch(ws)
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
    assert first["retries_left"] == 1

    SessionEngine(ws, config).next_task()
    second = SessionEngine(ws, config).report("T1", claimed_ok=False, error="compile error again")
    assert second["status"] == "blocked"
    assert second["retries_left"] == 0

    assert PlanDocument.load(ws.plan_path).get("T1").status == "blocked"


def test_report_without_verify_command_records_unverified(tmp_workspace):
    ws = _ws(tmp_workspace)
    SessionEngine(ws, {}).start()
    SessionEngine(ws, {}).next_task()
    result = SessionEngine(ws, {}).report("T1", claimed_ok=True)
    assert result["status"] == "completed"
    assert result["verified"] is False
    assert result["verify_command"] is None


def test_previous_failure_is_fed_back_into_the_next_briefing(tmp_workspace):
    ws = _ws(tmp_workspace)
    config = {"retry_budget": 2}
    SessionEngine(ws, config).start()
    SessionEngine(ws, config).next_task()
    SessionEngine(ws, config).report("T1", claimed_ok=False, error="missing import yaml")

    nxt = SessionEngine(ws, config).next_task()
    assert nxt["task"]["id"] == "T1"
    assert nxt["task"]["attempt"] == 2
    assert nxt["task"]["last_error"] == "missing import yaml"
    briefing = (ws.state_dir / "plan" / "current-task.md").read_text(encoding="utf-8")
    assert "missing import yaml" in briefing


def test_end_clears_the_session_and_reports_the_exit_gate(tmp_workspace):
    ws = _ws(tmp_workspace)
    SessionEngine(ws, {}).start()
    result = SessionEngine(ws, {}).end()
    assert result["status"] == "halted"
    assert result["completed"] == 0
    assert SessionState.load(ws) is None
