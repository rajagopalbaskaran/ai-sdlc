import subprocess
import time

from ai_sdlc.adapters.base import Adapter, AdapterResult
from ai_sdlc.adapters.mock import MockAdapter
from ai_sdlc.orchestrator.engine import Engine
from ai_sdlc.state.plan import PlanDocument
from ai_sdlc.workspace import Workspace

PLAN = """# Implementation Plan

### Task 1: First

```yaml
id: T1
status: pending
depends_on: []
persona: developer
```

Do first thing.

### Task 2: Second (independent of T3)

```yaml
id: T2
status: pending
depends_on: [T1]
persona: developer
```

Do second thing.

### Task 3: Third (independent of T2)

```yaml
id: T3
status: pending
depends_on: [T1]
persona: developer
```

Do third thing.
"""


def seed(tmp_workspace, plan_text=PLAN, approve_plan=True):
    ws = Workspace.init(tmp_workspace)
    ws.plan_path.write_text(plan_text, encoding="utf-8")
    if approve_plan:
        (ws.state_dir / "approvals.yaml").write_text("plan: true\n", encoding="utf-8")
    return ws


def approve_all(_prompt):
    return "a"


def test_sequential_run_completes(tmp_workspace):
    ws = seed(tmp_workspace)
    engine = Engine(ws, MockAdapter(), config={}, input_fn=approve_all)
    summary = engine.run(parallel=False)
    assert summary.status == "completed"
    assert summary.completed == 3
    doc = PlanDocument.load(ws.plan_path)
    assert all(t.status == "completed" for t in doc.tasks)


class SleepyAdapter(Adapter):
    name = "sleepy"

    def __init__(self, delay=0.2):
        self.delay = delay
        self.windows = {}

    def execute(self, persona, context, task):
        start = time.monotonic()
        time.sleep(self.delay)
        self.windows[task.id] = (start, time.monotonic())
        return AdapterResult(ok=True, output="done")


def test_parallel_run_overlaps_independent_tasks(tmp_workspace):
    ws = seed(tmp_workspace)
    adapter = SleepyAdapter()
    engine = Engine(ws, adapter, config={}, input_fn=approve_all)
    summary = engine.run(parallel=True)
    assert summary.status == "completed"
    # T2 and T3 are independent: their execution windows must overlap
    s2, e2 = adapter.windows["T2"]
    s3, e3 = adapter.windows["T3"]
    assert s2 < e3 and s3 < e2


def test_failure_retries_then_blocks(tmp_workspace):
    ws = seed(tmp_workspace)
    adapter = MockAdapter(
        script={
            "T1": [
                AdapterResult(ok=False, error="fail1"),
                AdapterResult(ok=False, error="fail2"),
                AdapterResult(ok=False, error="fail3"),
            ]
        }
    )
    engine = Engine(ws, adapter, config={"retry_budget": 2}, input_fn=approve_all)
    summary = engine.run(parallel=False)
    assert summary.status == "halted"
    doc = PlanDocument.load(ws.plan_path)
    assert doc.get("T1").status == "blocked"
    # T2/T3 unreachable, still pending
    assert doc.get("T2").status == "pending"
    audit_text = engine.audit.path.read_text(encoding="utf-8")
    assert audit_text.count('"retry"') == 2


class InterruptingAdapter(Adapter):
    name = "interrupting"

    def __init__(self):
        self.calls = 0

    def execute(self, persona, context, task):
        self.calls += 1
        if task.id == "T2":
            raise KeyboardInterrupt
        return AdapterResult(ok=True, output="done")


def test_safe_stop_and_resume(tmp_workspace):
    ws = seed(tmp_workspace)
    engine = Engine(ws, InterruptingAdapter(), config={}, input_fn=approve_all)
    summary = engine.run(parallel=False)
    assert summary.status == "stopped"
    # plan on disk is still parseable and T1 progress survived
    doc = PlanDocument.load(ws.plan_path)
    assert doc.get("T1").status == "completed"
    # resume with a working adapter finishes the rest
    engine2 = Engine(ws, MockAdapter(), config={}, input_fn=approve_all)
    summary2 = engine2.run(parallel=False)
    assert summary2.status == "completed"
    assert summary2.completed == 3


def test_run_works_on_feature_branch_and_records_it(tmp_workspace):
    ws = seed(tmp_workspace, plan_text=PLAN_ONE)
    _make_git_repo(tmp_workspace)
    engine = Engine(
        ws, WritingAdapter(tmp_workspace), config={"commit_mode": "auto"}, input_fn=approve_all
    )
    assert engine.run(parallel=False).status == "completed"
    # branch recorded in the plan (execution state)
    doc = PlanDocument.load(ws.plan_path)
    assert doc.meta.get("branch") == "feature/demo-app"
    # commits landed on the feature branch, main stays clean
    feature_log = _git(tmp_workspace, "log", "--format=%s", "feature/demo-app").stdout
    main_log = _git(tmp_workspace, "log", "--format=%s", "main").stdout
    assert "[ai-sdlc:T1]" in feature_log
    assert "[ai-sdlc:T1]" not in main_log
    # branch choice is recorded as a structured decision
    audit_text = engine.audit.path.read_text(encoding="utf-8")
    assert "branch_selection" in audit_text


def test_branch_recommendation_from_analysis_title(tmp_workspace):
    from ai_sdlc.orchestrator.engine import recommend_branch

    ws = seed(tmp_workspace, plan_text=PLAN_ONE)
    ws.analysis_path.write_text(
        "# Requirement Analysis: URL Shortener\n\ncontent\n", encoding="utf-8"
    )
    name, source = recommend_branch(ws)
    assert name == "feature/url-shortener"
    assert "analysis" in source


def test_branch_recommendation_detects_bug_fixes(tmp_workspace):
    from ai_sdlc.orchestrator.engine import recommend_branch

    ws = seed(tmp_workspace, plan_text=PLAN_ONE)
    ws.analysis_path.write_text(
        "# Bug: Duplicate short codes under concurrent requests\n\ncontent\n",
        encoding="utf-8",
    )
    name, _ = recommend_branch(ws)
    assert name.startswith("fix/")
    assert "duplicate-short-codes" in name


def test_branch_recommendation_falls_back_to_workspace_name(tmp_workspace):
    from ai_sdlc.orchestrator.engine import recommend_branch

    ws = seed(tmp_workspace, plan_text=PLAN_ONE)
    name, source = recommend_branch(ws)
    assert name == "feature/demo-app"
    assert "workspace" in source


class FakeTty:
    def isatty(self):
        return True


def test_interactive_branch_prompt_accepts_custom_name(tmp_workspace, monkeypatch):
    import sys

    ws = seed(tmp_workspace, plan_text=PLAN_ONE)
    _make_git_repo(tmp_workspace)
    monkeypatch.setattr(sys, "stdin", FakeTty())

    def answers(prompt):
        if "Branch" in prompt:
            return "feature/my-own-name"
        return "a"

    engine = Engine(ws, WritingAdapter(tmp_workspace), config={"commit_mode": "off"}, input_fn=answers)
    assert engine.run(parallel=False).status == "completed"
    assert PlanDocument.load(ws.plan_path).meta.get("branch") == "feature/my-own-name"


def test_interactive_branch_prompt_empty_accepts_recommendation(tmp_workspace, monkeypatch):
    import sys

    ws = seed(tmp_workspace, plan_text=PLAN_ONE)
    _make_git_repo(tmp_workspace)
    monkeypatch.setattr(sys, "stdin", FakeTty())

    def answers(prompt):
        if "Branch" in prompt:
            return ""
        return "a"

    engine = Engine(ws, WritingAdapter(tmp_workspace), config={"commit_mode": "off"}, input_fn=answers)
    assert engine.run(parallel=False).status == "completed"
    assert PlanDocument.load(ws.plan_path).meta.get("branch") == "feature/demo-app"


def test_push_gate_approved_pushes_to_remote(tmp_workspace, tmp_path):
    import subprocess

    ws = seed(tmp_workspace, plan_text=PLAN_ONE)
    _make_git_repo(tmp_workspace)
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _git(tmp_workspace, "remote", "add", "origin", str(bare))
    engine = Engine(
        ws, WritingAdapter(tmp_workspace), config={"commit_mode": "auto"}, input_fn=approve_all
    )
    assert engine.run(parallel=False).status == "completed"
    listed = subprocess.run(
        ["git", "--git-dir", str(bare), "branch", "--list"],
        capture_output=True,
        text=True,
    ).stdout
    assert "feature/demo-app" in listed
    assert '"push"' in engine.audit.path.read_text(encoding="utf-8")


def test_push_gate_rejected_never_pushes(tmp_workspace, tmp_path):
    import subprocess

    ws = seed(tmp_workspace, plan_text=PLAN_ONE)
    _make_git_repo(tmp_workspace)
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _git(tmp_workspace, "remote", "add", "origin", str(bare))
    engine = Engine(
        ws,
        WritingAdapter(tmp_workspace),
        config={"commit_mode": "off"},
        input_fn=lambda _: "r",  # plan pre-approved; push prompt rejected
    )
    assert engine.run(parallel=False).status == "completed"
    listed = subprocess.run(
        ["git", "--git-dir", str(bare), "branch", "--list"],
        capture_output=True,
        text=True,
    ).stdout
    assert "feature/demo-app" not in listed


def test_stale_analysis_halts_run(tmp_workspace):
    ws = seed(tmp_workspace)
    analysis = ws.state_dir / "plan" / "requirement-analysis.md"
    analysis.write_text("original analysis", encoding="utf-8")
    (ws.state_dir / "approvals.yaml").write_text(
        f"plan: true\nanalysis_sha: {ws.analysis_sha()}\n", encoding="utf-8"
    )
    # analysis changes after the plan was approved
    analysis.write_text("CHANGED analysis", encoding="utf-8")
    engine = Engine(ws, MockAdapter(), config={}, input_fn=approve_all)
    summary = engine.run(parallel=False)
    assert summary.status == "halted"
    assert summary.completed == 0
    audit_text = engine.audit.path.read_text(encoding="utf-8")
    assert "stale" in audit_text
    assert "stale_analysis" in audit_text  # structured decision record


def test_plan_approval_records_analysis_sha(tmp_workspace):
    ws = seed(tmp_workspace, approve_plan=False)
    analysis = ws.state_dir / "plan" / "requirement-analysis.md"
    analysis.write_text("the analysis", encoding="utf-8")
    engine = Engine(ws, MockAdapter(), config={}, input_fn=approve_all)
    assert engine.run(parallel=False).status == "completed"
    approvals = (ws.state_dir / "approvals.yaml").read_text(encoding="utf-8")
    assert "analysis_sha" in approvals
    assert ws.analysis_sha() in approvals


def test_plan_approval_gate_blocks_rejection(tmp_workspace):
    ws = seed(tmp_workspace, approve_plan=False)
    engine = Engine(ws, MockAdapter(), config={}, input_fn=lambda _: "r")
    summary = engine.run(parallel=False)
    assert summary.status == "halted"
    doc = PlanDocument.load(ws.plan_path)
    assert all(t.status == "pending" for t in doc.tasks)


PLAN_ONE = """# Implementation Plan

### Task 1: Only

```yaml
id: T1
status: pending
depends_on: []
persona: developer
```

Do the only thing.
"""


class WritingAdapter(Adapter):
    """Writes a real file into the workspace, like a real coding agent."""

    name = "writing"

    def __init__(self, root, content="print('hi')\n"):
        self.root = root
        self.content = content

    def execute(self, persona, context, task):
        path = self.root / f"src_{task.id}.py"
        path.write_text(self.content, encoding="utf-8")
        return AdapterResult(ok=True, output="wrote file")  # note: no files_changed reported


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _make_git_repo(root):
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "test")
    _git(root, "config", "user.email", "t@t.local")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")


def test_detected_secret_blocks_task(tmp_workspace):
    ws = seed(tmp_workspace, plan_text=PLAN_ONE)
    adapter = WritingAdapter(tmp_workspace, content='PASSWORD = "supersecret123"\n')
    engine = Engine(ws, adapter, config={"retry_budget": 0}, input_fn=approve_all)
    summary = engine.run(parallel=False)
    assert summary.status == "halted"
    assert PlanDocument.load(ws.plan_path).get("T1").status == "blocked"
    assert "policy" in engine.audit.path.read_text(encoding="utf-8")


def test_auto_commit_per_task_excludes_state(tmp_workspace):
    ws = seed(tmp_workspace, plan_text=PLAN_ONE)
    _make_git_repo(tmp_workspace)
    engine = Engine(
        ws, WritingAdapter(tmp_workspace), config={"commit_mode": "auto"}, input_fn=approve_all
    )
    assert engine.run(parallel=False).status == "completed"
    subjects = _git(tmp_workspace, "log", "--format=%s").stdout
    assert "[ai-sdlc:T1]" in subjects
    shown = _git(tmp_workspace, "show", "--name-only", "--format=", "HEAD").stdout
    assert "src_T1.py" in shown
    assert ".ai-sdlc" not in shown


def test_commit_mode_ask_rejection_skips_commit(tmp_workspace):
    ws = seed(tmp_workspace, plan_text=PLAN_ONE)
    _make_git_repo(tmp_workspace)
    engine = Engine(
        ws,
        WritingAdapter(tmp_workspace),
        config={"commit_mode": "ask"},
        input_fn=lambda _: "r",  # plan pre-approved in seed; only commit asks
    )
    assert engine.run(parallel=False).status == "completed"
    subjects = _git(tmp_workspace, "log", "--format=%s").stdout
    assert "[ai-sdlc:T1]" not in subjects


def test_no_git_repo_skips_commit_quietly(tmp_workspace):
    ws = seed(tmp_workspace, plan_text=PLAN_ONE)
    engine = Engine(
        ws, WritingAdapter(tmp_workspace), config={"commit_mode": "auto"}, input_fn=approve_all
    )
    assert engine.run(parallel=False).status == "completed"


def test_plan_approval_gate_accepts_and_persists(tmp_workspace):
    ws = seed(tmp_workspace, approve_plan=False)
    prompts = []

    def approver(prompt):
        prompts.append(prompt)
        return "a"

    engine = Engine(ws, MockAdapter(), config={}, input_fn=approver)
    assert engine.run(parallel=False).status == "completed"
    assert any("implementation plan" in p.lower() for p in prompts)
    # second run must not re-ask: approval persisted
    engine2 = Engine(ws, MockAdapter(), config={}, input_fn=lambda _: "r")
    assert engine2.run(parallel=False).status == "completed"
