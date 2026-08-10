"""Full pipeline integration test on the Mock adapter - no LLM required."""

from ai_sdlc.adapters.mock import MockAdapter
from ai_sdlc.observability.metrics import compute_metrics
from ai_sdlc.observability.report import render_report
from ai_sdlc.orchestrator.engine import Engine
from ai_sdlc.state.plan import PlanDocument
from ai_sdlc.workspace import Workspace

PLAN = """# Implementation Plan

### Task 1: Schema

```yaml
id: T1
status: pending
depends_on: []
persona: developer
derived_from: [requirement-analysis.md#storage]
```

Create the database schema.

### Task 2: API

```yaml
id: T2
status: pending
depends_on: [T1]
persona: developer
derived_from: [requirement-analysis.md#api]
```

Build the endpoint.

### Task 3: Tests

```yaml
id: T3
status: pending
depends_on: [T2]
persona: tester
derived_from: [requirement-analysis.md#quality]
```

Run the suite.
"""


def test_full_pipeline_on_mock(tmp_workspace):
    ws = Workspace.init(tmp_workspace)
    ws.plan_path.write_text(PLAN, encoding="utf-8")
    (ws.state_dir / "project-profile.md").write_text(
        "# Project Profile\n\n- Language: Python\n", encoding="utf-8"
    )
    (ws.state_dir / "knowledge-base" / "architecture.md").write_text(
        "# Architecture\n\nSingle service.\n", encoding="utf-8"
    )

    engine = Engine(ws, MockAdapter(), config={"approval_gates": ["plan", "deploy_ready"]}, input_fn=lambda _: "a")
    summary = engine.run(parallel=False)

    assert summary.status == "completed"
    assert summary.completed == 3
    assert all(t.status == "completed" for t in PlanDocument.load(ws.plan_path).tasks)

    # audit log exists, is valid, and metrics compute
    assert engine.audit.path.is_file()
    metrics = compute_metrics(engine.audit.path)
    assert metrics["success_rate"] == 1.0
    assert metrics["tasks_completed"] == 3

    # report renders
    md = render_report(engine.audit.path)
    assert "Success rate: 100.0%" in md
    assert "task_completed" in md


def test_full_session_pipeline_in_a_git_repo(tmp_workspace, capsys):
    """Session mode end to end through the real CLI: approve, pin a branch,
    start, then next/report per task. Proves the per-task commits land on the
    pinned branch and that one run id covers the whole session."""
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
        reported = json.loads(capsys.readouterr().out)
        assert reported["status"] == "completed"
        assert reported["verified"] is True

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

    events = [
        json.loads(line)
        for path in (ws.state_dir / "runs").glob("audit-*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # one session run id covers both tasks: the audit trail is not fragmented
    run_ids = {
        e["run_id"] for e in events if e["type"] in ("task_started", "task_completed")
    }
    assert len(run_ids) == 1


def test_session_blocks_a_task_whose_verification_keeps_failing(tmp_workspace, capsys):
    """The guardrail that matters: a session claiming pass cannot complete a
    task whose verify command fails, and the retry budget still terminates."""
    import json
    import subprocess

    from ai_sdlc.cli import main
    from ai_sdlc.state.plan import PlanDocument
    from ai_sdlc.workspace import Workspace

    subprocess.run(["git", "init", "-q"], cwd=tmp_workspace, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_workspace, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_workspace, check=True)

    main(["init", "--workspace", str(tmp_workspace)])
    ws = Workspace(tmp_workspace)
    ws.plan_path.write_text(
        "# Implementation Plan\n\n"
        "### Task 1: One\n\n"
        "```yaml\nid: T1\nstatus: pending\ndepends_on: []\npersona: developer\n```\n\n"
        "Create app.py\n",
        encoding="utf-8",
    )
    (ws.state_dir / "config.yaml").write_text(
        'adapter: session\nretry_budget: 1\ntask_verify_command: "exit 7"\n', encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=tmp_workspace, check=True)

    main(["approve", "--gate", "plan", "--workspace", str(tmp_workspace)])
    main(["branch", "--use", "fix/verify", "--workspace", str(tmp_workspace)])
    main(["session", "start", "--workspace", str(tmp_workspace)])
    capsys.readouterr()

    statuses = []
    for _ in range(2):
        main(["next", "--json", "--workspace", str(tmp_workspace)])
        capsys.readouterr()
        main([
            "report-task", "--task", "T1", "--result", "pass",
            "--json", "--workspace", str(tmp_workspace),
        ])
        statuses.append(json.loads(capsys.readouterr().out)["status"])

    assert statuses == ["pending", "blocked"]
    assert PlanDocument.load(ws.plan_path).get("T1").status == "blocked"

    assert main(["session", "end", "--json", "--workspace", str(tmp_workspace)]) == 1
    end = json.loads(capsys.readouterr().out)
    assert end["status"] == "halted"
    assert end["blocked"] == 1
