import json

from ai_sdlc.cli import main
from ai_sdlc.observability.summary import generate_summary
from ai_sdlc.workspace import Workspace

PLAN = """# Implementation Plan

```yaml
branch: feature/demo-app
```

### Task 1: Build schema

```yaml
id: T1
status: completed
depends_on: []
persona: developer
derived_from: [requirement-analysis.md#storage]
```

Schema work.

### Task 2: API endpoint

```yaml
id: T2
status: blocked
depends_on: [T1]
persona: developer
```

Endpoint work.
"""

ANALYSIS = """# Requirement Analysis

## Functional analysis

Users shorten URLs.

## Risks and trade-offs

- Collision risk on generated codes
- MySQL unavailable in dev

## Assumptions

- Codes are 6 characters
- No authentication required
"""

AUDIT = [
    {"ts": 100.0, "run_id": "r1", "type": "run_started"},
    {"ts": 100.5, "run_id": "r1", "type": "stage_started", "stage": "develop"},
    {"ts": 101.0, "run_id": "r1", "type": "approval", "gate": "plan", "decision": "approve"},
    {"ts": 102.0, "run_id": "r1", "type": "task_started", "task": "T1"},
    {"ts": 103.0, "run_id": "r1", "type": "task_failed", "task": "T1"},
    {"ts": 104.0, "run_id": "r1", "type": "retry", "task": "T1"},
    {"ts": 110.0, "run_id": "r1", "type": "task_completed", "task": "T1"},
    {"ts": 111.0, "run_id": "r1", "type": "stage_completed", "stage": "develop"},
    {"ts": 112.0, "run_id": "r1", "type": "run_completed"},
]


def seed(tmp_workspace):
    ws = Workspace.init(tmp_workspace)
    ws.plan_path.write_text(PLAN, encoding="utf-8")
    ws.analysis_path.write_text(ANALYSIS, encoding="utf-8")
    (ws.state_dir / "approvals.yaml").write_text("plan: true\n", encoding="utf-8")
    log = ws.runs_dir / "audit-r1.jsonl"
    log.write_text("\n".join(json.dumps(e) for e in AUDIT) + "\n", encoding="utf-8")
    return ws


def test_summary_covers_plan_execution_risks_assumptions(tmp_workspace):
    ws = seed(tmp_workspace)
    md = generate_summary(ws)
    # plan and rationale
    assert "feature/demo-app" in md
    assert "T1" in md and "Build schema" in md
    assert "requirement-analysis.md#storage" in md
    # execution and reliability
    assert "completed: 1" in md
    assert "blocked: 1" in md
    assert "Retries: 1" in md
    # approvals
    assert "plan" in md and "approve" in md
    # extracted sections from the analysis
    assert "Collision risk" in md
    assert "No authentication required" in md
    # limitations name the blocked task
    assert "T2" in md


def test_summary_handles_missing_artifacts(tmp_workspace):
    ws = Workspace.init(tmp_workspace)
    md = generate_summary(ws)
    assert "Engineering Summary" in md
    assert "no requirement analysis" in md.lower()


def test_cli_summarize_writes_file(tmp_workspace, capsys):
    seed(tmp_workspace)
    assert main(["summarize", "--workspace", str(tmp_workspace)]) == 0
    out_path = tmp_workspace / ".ai-sdlc" / "engineering-summary.md"
    assert out_path.is_file()
    assert "Engineering Summary" in out_path.read_text(encoding="utf-8")
