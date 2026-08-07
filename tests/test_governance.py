import subprocess

import pytest

from ai_sdlc.adapters.base import AdapterResult
from ai_sdlc.adapters.mock import MockAdapter
from ai_sdlc.governance.approvals import request_approval
from ai_sdlc.governance.fallback import FallbackChain
from ai_sdlc.governance.policy import check_policies
from ai_sdlc.governance.retry import RetryPolicy
from ai_sdlc.governance.rollback import commit_task, rollback_task
from ai_sdlc.state.plan import Task


def make_task(tid="T1"):
    return Task(id=tid, title="demo", status="pending")


# --- approvals ---

def test_approval_parses_short_and_full_words():
    assert request_approval("ok?", input_fn=lambda _: "a") == "approve"
    assert request_approval("ok?", input_fn=lambda _: "approve") == "approve"
    assert request_approval("ok?", input_fn=lambda _: "r") == "reject"
    assert request_approval("ok?", input_fn=lambda _: "m") == "modify"


def test_approval_reprompts_on_garbage():
    answers = iter(["what", "a"])
    assert request_approval("ok?", input_fn=lambda _: next(answers)) == "approve"


def test_approval_eof_means_reject():
    def raise_eof(_):
        raise EOFError

    assert request_approval("ok?", input_fn=raise_eof) == "reject"


# --- retry ---

def test_retry_succeeds_within_budget():
    calls = []

    def flaky(last_error):
        calls.append(last_error)
        if len(calls) < 3:
            return AdapterResult(ok=False, error=f"fail{len(calls)}")
        return AdapterResult(ok=True, output="done")

    events = []
    result = RetryPolicy(budget=2).attempt(
        make_task(), flaky, on_retry=lambda **kw: events.append(kw)
    )
    assert result.ok
    assert calls == [None, "fail1", "fail2"]  # error context fed forward
    assert len(events) == 2


def test_retry_exhausts_budget():
    def always_fail(last_error):
        return AdapterResult(ok=False, error="boom")

    result = RetryPolicy(budget=2).attempt(make_task(), always_fail)
    assert not result.ok


# --- fallback ---

def test_fallback_chain_switches_adapter():
    failing = MockAdapter(script={"T1": [AdapterResult(ok=False, error="down")]})
    working = MockAdapter()
    switches = []
    chain = FallbackChain([failing, working], on_fallback=lambda **kw: switches.append(kw))
    result = chain.execute("developer", "ctx", make_task())
    assert result.ok
    assert len(switches) == 1


def test_fallback_chain_all_fail():
    a1 = MockAdapter(script={"T1": [AdapterResult(ok=False, error="e1")]})
    a2 = MockAdapter(script={"T1": [AdapterResult(ok=False, error="e2")]})
    result = FallbackChain([a1, a2]).execute("developer", "ctx", make_task())
    assert not result.ok


# --- policy ---

def test_policy_flags_secrets_and_outside_writes(tmp_path):
    inside = tmp_path / "app.py"
    inside.write_text('API_KEY = "sk-abc123secret"\n', encoding="utf-8")
    outside = tmp_path.parent / "evil.py"
    violations = check_policies([str(inside), str(outside)], tmp_path)
    assert any("secret" in v.lower() for v in violations)
    assert any("outside" in v.lower() for v in violations)


def test_policy_clean_file_passes(tmp_path):
    clean = tmp_path / "app.py"
    clean.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    assert check_policies([str(clean)], tmp_path) == []


# --- rollback ---

@pytest.fixture
def git_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.local"], cwd=tmp_path, check=True)
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=tmp_path, check=True)
    return tmp_path


def test_commit_and_rollback_task(git_repo):
    (git_repo / "feature.txt").write_text("bad change\n", encoding="utf-8")
    assert commit_task(git_repo, "T7", "add feature")
    assert (git_repo / "feature.txt").exists()
    assert rollback_task(git_repo, "T7")
    assert not (git_repo / "feature.txt").exists()


def test_rollback_missing_task_returns_false(git_repo):
    assert not rollback_task(git_repo, "T99")
