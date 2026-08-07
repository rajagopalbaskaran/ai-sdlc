import json

from ai_sdlc.observability.audit import AuditLog


def test_events_append_as_jsonl(tmp_path):
    log = AuditLog(tmp_path, "run1")
    log.event("run_started")
    log.event("task_started", task="T1", persona="developer")
    log.event("task_completed", task="T1")

    lines = log.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    events = [json.loads(line) for line in lines]
    assert [e["type"] for e in events] == ["run_started", "task_started", "task_completed"]
    assert all(e["run_id"] == "run1" for e in events)
    assert all("ts" in e for e in events)
    assert events[1]["task"] == "T1"


def test_log_is_append_only(tmp_path):
    log = AuditLog(tmp_path, "run1")
    log.event("run_started")
    first = log.path.read_text(encoding="utf-8")
    log.event("run_completed")
    combined = log.path.read_text(encoding="utf-8")
    assert combined.startswith(first)
