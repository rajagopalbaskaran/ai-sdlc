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
    capsys.readouterr()

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
    capsys.readouterr()

    rc = main(["branch", "--use", "fix/demo", "--json", "--workspace", str(tmp_workspace)])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["pinned"] == "fix/demo"
    assert data["current"] == "fix/demo"
    assert data["created"] is True

    doc = PlanDocument.load(Workspace(tmp_workspace).plan_path)
    assert doc.meta["branch"] == "fix/demo"


def test_approve_plan_records_analysis_fingerprint(tmp_workspace, capsys):
    import yaml

    state = _seed(tmp_workspace)
    (state / "approvals.yaml").write_text("plan: false\n", encoding="utf-8")
    (state / "plan" / "requirement-analysis.md").write_text("# Analysis\n", encoding="utf-8")
    capsys.readouterr()

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
    capsys.readouterr()

    rc = main(["session", "start", "--json", "--workspace", str(tmp_workspace)])
    assert rc == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert any("branch" in r for r in data["reasons"])


def test_session_status_reports_no_active_session(tmp_workspace, capsys):
    _seed(tmp_workspace)
    capsys.readouterr()

    assert main(["session", "status", "--json", "--workspace", str(tmp_workspace)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["active"] is False
    assert data["run_id"] is None
