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
