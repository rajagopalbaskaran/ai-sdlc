from ai_sdlc.orchestrator.gates import GateContext, entry_gate, exit_gate
from ai_sdlc.state.plan import Task


def make(tid, status="pending", deps=None):
    return Task(id=tid, title=tid, status=status, depends_on=deps or [])


def test_develop_entry_requires_plan_tasks():
    ctx = GateContext(tasks=[], approvals={"plan": True})
    result = entry_gate("develop", ctx)
    assert not result.passed
    assert any("no tasks" in r for r in result.reasons)


def test_develop_entry_requires_plan_approval():
    ctx = GateContext(tasks=[make("T1")], approvals={})
    result = entry_gate("develop", ctx)
    assert not result.passed


def test_develop_entry_passes():
    ctx = GateContext(tasks=[make("T1")], approvals={"plan": True})
    assert entry_gate("develop", ctx).passed


def test_develop_exit_fails_with_blocked_tasks():
    ctx = GateContext(tasks=[make("T1", status="completed"), make("T2", status="blocked")], approvals={"plan": True})
    result = exit_gate("develop", ctx)
    assert not result.passed
    assert any("T2" in r for r in result.reasons)


def test_develop_exit_passes_when_all_completed():
    ctx = GateContext(tasks=[make("T1", status="completed")], approvals={"plan": True})
    assert exit_gate("develop", ctx).passed
