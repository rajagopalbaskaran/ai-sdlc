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


def test_echo_prints_simple_lines(tmp_path, capsys):
    log = AuditLog(tmp_path, "r1", echo=True)
    log.event("task_started", task="T1", persona="developer", title="Build schema")
    log.event("task_completed", task="T1", files_changed=2, seconds=15)
    log.event("gate", stage="develop", kind="entry", passed=True)  # silent
    out = capsys.readouterr().out
    assert "T1 started - Build schema (developer)" in out
    assert "T1 completed (2 files, 15s)" in out
    assert "gate" not in out


def test_no_echo_by_default(tmp_path, capsys):
    log = AuditLog(tmp_path, "r1")
    log.event("task_started", task="T1")
    assert capsys.readouterr().out == ""


def test_log_is_append_only(tmp_path):
    log = AuditLog(tmp_path, "run1")
    log.event("run_started")
    first = log.path.read_text(encoding="utf-8")
    log.event("run_completed")
    combined = log.path.read_text(encoding="utf-8")
    assert combined.startswith(first)
