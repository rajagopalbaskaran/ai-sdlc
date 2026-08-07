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


def test_cli_status_empty_plan(tmp_workspace, capsys):
    main(["init", "--workspace", str(tmp_workspace)])
    assert main(["status", "--workspace", str(tmp_workspace)]) == 0
    assert "no tasks" in capsys.readouterr().out
