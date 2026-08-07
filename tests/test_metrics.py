import json

from ai_sdlc.observability.metrics import compute_metrics
from ai_sdlc.observability.report import render_report


def write_log(path, events):
    path.write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )


SYNTHETIC = [
    {"ts": 100.0, "run_id": "r1", "type": "run_started"},
    {"ts": 101.0, "run_id": "r1", "type": "task_started", "task": "T1"},
    {"ts": 102.0, "run_id": "r1", "type": "task_completed", "task": "T1"},
    {"ts": 103.0, "run_id": "r1", "type": "task_started", "task": "T2"},
    {"ts": 104.0, "run_id": "r1", "type": "task_failed", "task": "T2"},
    {"ts": 105.0, "run_id": "r1", "type": "retry", "task": "T2"},
    {"ts": 110.0, "run_id": "r1", "type": "task_completed", "task": "T2"},
    {"ts": 111.0, "run_id": "r1", "type": "task_started", "task": "T3"},
    {"ts": 112.0, "run_id": "r1", "type": "task_failed", "task": "T3"},
    {"ts": 113.0, "run_id": "r1", "type": "rollback", "task": "T3"},
    {"ts": 120.0, "run_id": "r1", "type": "run_completed"},
]


def test_metrics_from_synthetic_log(tmp_path):
    log = tmp_path / "audit-r1.jsonl"
    write_log(log, SYNTHETIC)
    m = compute_metrics(log)
    assert m["tasks_started"] == 3
    assert m["tasks_completed"] == 2
    assert m["success_rate"] == 2 / 3
    assert m["retries"] == 1
    assert m["rollbacks"] == 1
    # T2 failed at 104, recovered at 110 -> only recovery observed, MTTR 6s
    assert m["mttr_seconds"] == 6.0
    assert m["e2e_seconds"] == 20.0


def test_report_renders_markdown(tmp_path):
    log = tmp_path / "audit-r1.jsonl"
    write_log(log, SYNTHETIC)
    md = render_report(log)
    assert "Success rate" in md
    assert "66.7%" in md
    assert "rollback" in md.lower()
