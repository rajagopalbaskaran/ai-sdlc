import pytest

from ai_sdlc.cli import main

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


def test_cli_init_exit_zero(tmp_workspace):
    rc = main(["init", "--workspace", str(tmp_workspace)])
    assert rc == 0
    assert (tmp_workspace / ".ai-sdlc").is_dir()


def test_cli_init_twice_fails(tmp_workspace):
    assert main(["init", "--workspace", str(tmp_workspace)]) == 0
    assert main(["init", "--workspace", str(tmp_workspace)]) == 1


def _seed(tmp_workspace):
    main(["init", "--workspace", str(tmp_workspace)])
    state = tmp_workspace / ".ai-sdlc"
    (state / "plan" / "implementation-plan.md").write_text(PLAN, encoding="utf-8")
    (state / "approvals.yaml").write_text("plan: true\ndeploy_ready: true\n", encoding="utf-8")
    return state


def test_cli_run_status_report(tmp_workspace, capsys):
    state = _seed(tmp_workspace)

    assert main(["run", "--workspace", str(tmp_workspace)]) == 0
    out = capsys.readouterr().out
    assert "completed=1" in out

    assert main(["status", "--workspace", str(tmp_workspace)]) == 0
    out = capsys.readouterr().out
    assert "T1" in out and "completed" in out

    assert main(["report", "--workspace", str(tmp_workspace)]) == 0
    out = capsys.readouterr().out
    assert "success_rate=100.0%" in out
    assert list((state / "runs").glob("report-*.md"))


def test_warn_unexpected_changes_detects_and_audits(tmp_workspace, capsys):
    from ai_sdlc.changes import snapshot
    from ai_sdlc.cli import _warn_unexpected_changes
    from ai_sdlc.observability.audit import AuditLog
    from ai_sdlc.workspace import Workspace

    main(["init", "--workspace", str(tmp_workspace)])
    ws = Workspace(tmp_workspace)
    audit = AuditLog(ws.runs_dir, "guard-test")
    before = snapshot(tmp_workspace)
    (tmp_workspace / "sneaky.txt").write_text("should not happen", encoding="utf-8")
    _warn_unexpected_changes(ws, audit, "analyze", before)
    assert "unexpectedly changed 1 file" in capsys.readouterr().err
    assert "policy_warning" in audit.path.read_text(encoding="utf-8")


def test_warn_unexpected_changes_silent_when_clean(tmp_workspace, capsys):
    from ai_sdlc.changes import snapshot
    from ai_sdlc.cli import _warn_unexpected_changes
    from ai_sdlc.observability.audit import AuditLog
    from ai_sdlc.workspace import Workspace

    main(["init", "--workspace", str(tmp_workspace)])
    ws = Workspace(tmp_workspace)
    audit = AuditLog(ws.runs_dir, "guard-clean")
    before = snapshot(tmp_workspace)
    _warn_unexpected_changes(ws, audit, "analyze", before)
    assert capsys.readouterr().err == ""


def test_cli_retry_resets_blocked_task(tmp_workspace, capsys):
    from ai_sdlc.state.plan import PlanDocument

    state = _seed(tmp_workspace)
    plan_path = state / "plan" / "implementation-plan.md"
    doc = PlanDocument.load(plan_path)
    doc.set_status("T1", "blocked")
    doc.save()
    assert main(["retry", "T1", "--workspace", str(tmp_workspace)]) == 0
    assert PlanDocument.load(plan_path).get("T1").status == "pending"
    logs = list((state / "runs").glob("audit-*.jsonl"))
    assert any("task_retry" in p.read_text(encoding="utf-8") for p in logs)


def test_cli_retry_rejects_non_blocked_task(tmp_workspace):
    from ai_sdlc.state.plan import PlanDocument

    state = _seed(tmp_workspace)
    plan_path = state / "plan" / "implementation-plan.md"
    assert main(["retry", "T1", "--workspace", str(tmp_workspace)]) == 1
    assert PlanDocument.load(plan_path).get("T1").status == "pending"


def test_analyze_revokes_prior_plan_approval(tmp_workspace):
    import yaml

    from ai_sdlc.state.plan import PlanDocument

    state = _seed(tmp_workspace)  # writes approvals plan: true
    plan_path = state / "plan" / "implementation-plan.md"
    doc = PlanDocument.load(plan_path)
    doc.set_meta(branch="feature/old-work")
    doc.save()
    req = tmp_workspace / "new-requirement.md"
    req.write_text("# New requirement\n\nDo something else.\n", encoding="utf-8")
    assert main(["analyze", str(req), "--workspace", str(tmp_workspace)]) == 0
    approvals = yaml.safe_load((state / "approvals.yaml").read_text(encoding="utf-8"))
    assert approvals.get("plan") is False
    # the old branch pin is released so the new requirement gets its own branch
    assert not PlanDocument.load(plan_path).meta.get("branch")


def test_cli_develop_is_primary_and_run_is_alias(tmp_workspace, capsys):
    _seed(tmp_workspace)
    assert main(["develop", "--workspace", str(tmp_workspace)]) == 0
    out = capsys.readouterr().out
    assert "completed=1" in out


def test_cli_status_empty_plan(tmp_workspace, capsys):
    main(["init", "--workspace", str(tmp_workspace)])
    assert main(["status", "--workspace", str(tmp_workspace)]) == 0
    assert "no tasks" in capsys.readouterr().out


def _seed_completed_committed_task(tmp_workspace):
    """Workspace with a git repo and one completed task whose change is
    committed with the [ai-sdlc:T1] marker."""
    import subprocess

    from ai_sdlc.governance.rollback import commit_task
    from ai_sdlc.state.plan import PlanDocument

    state = _seed(tmp_workspace)
    subprocess.run(["git", "init", "-q"], cwd=tmp_workspace, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_workspace, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.local"], cwd=tmp_workspace, check=True)
    (tmp_workspace / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_workspace, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=tmp_workspace, check=True)

    (tmp_workspace / "feature.py").write_text("x = 1\n", encoding="utf-8")
    assert commit_task(tmp_workspace, "T1", "the feature")
    doc = PlanDocument.load(state / "plan" / "implementation-plan.md")
    doc.set_status("T1", "completed")
    doc.save()
    return state


def test_cli_rollback_reverts_and_updates_status(tmp_workspace, capsys):
    from ai_sdlc.state.plan import PlanDocument

    state = _seed_completed_committed_task(tmp_workspace)
    rc = main(["rollback", "T1", "--yes", "--workspace", str(tmp_workspace)])
    assert rc == 0
    assert not (tmp_workspace / "feature.py").exists()
    doc = PlanDocument.load(state / "plan" / "implementation-plan.md")
    assert doc.get("T1").status == "rolled_back"
    # audited
    logs = list((state / "runs").glob("audit-*.jsonl"))
    assert any("rollback" in p.read_text(encoding="utf-8") for p in logs)


def test_cli_rollback_unknown_task_fails(tmp_workspace):
    _seed_completed_committed_task(tmp_workspace)
    assert main(["rollback", "T99", "--yes", "--workspace", str(tmp_workspace)]) == 1


REPLAN_PLAN = """# Implementation Plan

### Task 1: Done work

```yaml
id: T1
status: completed
depends_on: []
persona: developer
```

Already built.

### Task 2: Stale work

```yaml
id: T2
status: pending
depends_on: [T1]
persona: developer
```

Old approach.
"""

PROPOSAL = """### Task 2: Revised work

```yaml
id: T2
status: pending
depends_on: [T1]
persona: developer
```

New approach after requirement change.

### Task 3: Extra work

```yaml
id: T3
status: pending
depends_on: [T2]
persona: developer
```

Brand new task.
"""


def test_cli_replan_applies_proposal(tmp_workspace, capsys):
    from ai_sdlc.state.plan import PlanDocument

    state = _seed(tmp_workspace)
    plan_path = state / "plan" / "implementation-plan.md"
    plan_path.write_text(REPLAN_PLAN, encoding="utf-8")
    (state / "plan" / "requirement-analysis.md").write_text("v2 analysis", encoding="utf-8")
    proposal = tmp_workspace / "proposal.md"
    proposal.write_text(PROPOSAL, encoding="utf-8")

    rc = main(["replan", "--proposal", str(proposal), "--yes", "--workspace", str(tmp_workspace)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "keep" in out and "T1" in out

    doc = PlanDocument.load(plan_path)
    assert doc.get("T1").status == "completed"
    assert doc.get("T1").body == "Already built."
    assert "New approach" in doc.get("T2").body
    assert doc.get("T3").depends_on == ["T2"]
    # plan re-approved with fresh analysis sha -> run proceeds
    assert main(["run", "--workspace", str(tmp_workspace)]) == 0


def test_cli_replan_rejection_changes_nothing(tmp_workspace, monkeypatch):
    from ai_sdlc.state.plan import PlanDocument

    state = _seed(tmp_workspace)
    plan_path = state / "plan" / "implementation-plan.md"
    plan_path.write_text(REPLAN_PLAN, encoding="utf-8")
    proposal = tmp_workspace / "proposal.md"
    proposal.write_text(PROPOSAL, encoding="utf-8")

    # no --yes, non-interactive stdin -> reject
    rc = main(["replan", "--proposal", str(proposal), "--workspace", str(tmp_workspace)])
    assert rc == 1
    doc = PlanDocument.load(plan_path)
    assert "Old approach" in doc.get("T2").body
    with pytest.raises(KeyError):
        doc.get("T3")


def test_cli_rollback_without_yes_rejects_noninteractive(tmp_workspace):
    from ai_sdlc.state.plan import PlanDocument

    state = _seed_completed_committed_task(tmp_workspace)
    # no --yes and no interactive stdin -> approval defaults to reject
    assert main(["rollback", "T1", "--workspace", str(tmp_workspace)]) == 1
    assert (tmp_workspace / "feature.py").exists()
    doc = PlanDocument.load(state / "plan" / "implementation-plan.md")
    assert doc.get("T1").status == "completed"
