import pytest

from ai_sdlc.orchestrator.dag import CycleError
from ai_sdlc.orchestrator.replan import extract_header, merge, render_plan
from ai_sdlc.state.plan import PlanDocument, Task


def make(tid, status="pending", deps=None, body="body", title=None, persona="developer"):
    return Task(
        id=tid,
        title=title or f"Task {tid}",
        status=status,
        depends_on=deps or [],
        persona=persona,
        body=body,
    )


def test_merge_keeps_completed_revises_pending_adds_new():
    current = [
        make("T1", status="completed", body="old work done"),
        make("T2", status="pending", body="stale approach"),
        make("T3", status="pending", body="unchanged work"),
        make("T4", status="pending", body="no longer needed"),
    ]
    proposed = [
        make("T1", body="attempt to rewrite completed work"),  # must be ignored
        make("T2", body="revised approach", deps=["T1"]),
        make("T3", body="unchanged work"),
        make("T5", body="brand new task", deps=["T2"]),
    ]
    diff = merge(current, proposed)
    assert diff.keep == ["T1"]
    assert diff.revised == ["T2"]
    assert diff.unchanged == ["T3"]
    assert diff.dropped == ["T4"]
    assert diff.added == ["T5"]
    merged = {t.id: t for t in diff.merged}
    assert merged["T1"].body == "old work done"  # completed never touched
    assert merged["T1"].status == "completed"
    assert merged["T2"].body == "revised approach"
    assert merged["T2"].status == "pending"
    assert merged["T5"].status == "pending"
    assert "T4" not in merged


def test_merge_blocked_tasks_are_revisable():
    current = [make("T1", status="blocked", body="wrong plan")]
    proposed = [make("T1", body="fixed plan")]
    diff = merge(current, proposed)
    assert diff.revised == ["T1"]
    assert diff.merged[0].status == "pending"  # revised blocked task is re-runnable


def test_merge_rejects_cycles():
    current = [make("T1", status="completed")]
    proposed = [make("T2", deps=["T3"]), make("T3", deps=["T2"])]
    with pytest.raises(CycleError):
        merge(current, proposed)


def test_render_roundtrip(tmp_path):
    header = "# Implementation Plan\n\nIntro prose here.\n"
    tasks = [
        make("T1", status="completed", body="done work", title="Task 1: Done"),
        make("T2", status="pending", deps=["T1"], body="todo work", title="Task 2: Todo"),
    ]
    text = render_plan(header, tasks)
    path = tmp_path / "plan.md"
    path.write_text(text, encoding="utf-8")
    doc = PlanDocument.load(path)
    assert [t.id for t in doc.tasks] == ["T1", "T2"]
    assert doc.get("T1").status == "completed"
    assert doc.get("T2").depends_on == ["T1"]
    assert "Intro prose here." in text


def test_extract_header():
    text = "# Plan\n\nprose\n\n### Task 1: X\n\n```yaml\nid: T1\n```\n\nbody\n"
    assert extract_header(text) == "# Plan\n\nprose\n\n"
    assert extract_header("# Only header\n") == "# Only header\n"
