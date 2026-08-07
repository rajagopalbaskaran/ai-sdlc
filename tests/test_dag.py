import pytest

from ai_sdlc.orchestrator.dag import CycleError, detect_cycles, eligible_tasks, terminal
from ai_sdlc.state.plan import Task


def make(tid, status="pending", deps=None):
    return Task(id=tid, title=tid, status=status, depends_on=deps or [])


def test_diamond_eligibility():
    # T1 -> (T2, T3) -> T4
    tasks = [
        make("T1", status="completed"),
        make("T2", deps=["T1"]),
        make("T3", deps=["T1"]),
        make("T4", deps=["T2", "T3"]),
    ]
    assert [t.id for t in eligible_tasks(tasks)] == ["T2", "T3"]


def test_not_eligible_until_all_deps_complete():
    tasks = [
        make("T1", status="completed"),
        make("T2", status="in_progress", deps=["T1"]),
        make("T3", deps=["T1", "T2"]),
    ]
    assert eligible_tasks(tasks) == []


def test_cycle_detection_raises():
    tasks = [make("T1", deps=["T2"]), make("T2", deps=["T1"])]
    with pytest.raises(CycleError):
        detect_cycles(tasks)


def test_no_cycle_passes():
    tasks = [make("T1"), make("T2", deps=["T1"])]
    detect_cycles(tasks)


def test_unknown_dependency_raises():
    with pytest.raises(CycleError):
        detect_cycles([make("T1", deps=["T99"])])


def test_terminal_when_no_runnable_work():
    done = [make("T1", status="completed"), make("T2", status="blocked")]
    assert terminal(done)
    assert not terminal([make("T1")])
    # blocked dependency makes dependent unreachable -> terminal
    stuck = [make("T1", status="blocked"), make("T2", deps=["T1"])]
    assert terminal(stuck)
