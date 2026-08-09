import pytest

from ai_sdlc.adapters.base import AdapterResult, build_adapter
from ai_sdlc.adapters.claude_code import ClaudeCodeAdapter
from ai_sdlc.adapters.mock import MockAdapter
from ai_sdlc.state.plan import Task


def make_task(tid="T1"):
    return Task(id=tid, title="demo", status="pending")


def test_mock_default_success():
    adapter = MockAdapter()
    result = adapter.execute("developer", "ctx", make_task())
    assert result.ok
    assert "T1" in result.output


def test_mock_scripted_failure_then_success():
    adapter = MockAdapter(
        script={
            "T1": [
                AdapterResult(ok=False, output="", error="boom"),
                AdapterResult(ok=True, output="fixed"),
            ]
        }
    )
    first = adapter.execute("developer", "ctx", make_task())
    second = adapter.execute("developer", "ctx", make_task())
    assert not first.ok and first.error == "boom"
    assert second.ok and second.output == "fixed"


def test_factory_builds_by_name():
    assert isinstance(build_adapter("mock", {}), MockAdapter)
    assert isinstance(build_adapter("claude-code", {}), ClaudeCodeAdapter)
    with pytest.raises(ValueError):
        build_adapter("unknown", {})


def test_claude_code_argv_and_missing_binary(monkeypatch, tmp_path):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        raise FileNotFoundError("claude not found")

    monkeypatch.setattr("subprocess.run", fake_run)
    adapter = ClaudeCodeAdapter(command="claude", workdir=tmp_path)
    result = adapter.execute("developer", "some context", make_task())
    assert not result.ok
    assert "claude" in captured["argv"][0]
    assert "-p" in captured["argv"]
    assert result.error is not None


def test_text_personas_denied_edit_permission(monkeypatch, tmp_path):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        raise FileNotFoundError("claude not found")

    monkeypatch.setattr("subprocess.run", fake_run)
    adapter = ClaudeCodeAdapter(workdir=tmp_path)
    for persona in ("requirement_analyst", "implementation_planner", "validator"):
        adapter.execute(persona, "ctx", make_task())
        assert "--permission-mode" not in captured["argv"], persona
    for persona in ("developer", "tester", "deployment_engineer"):
        adapter.execute(persona, "ctx", make_task())
        assert "--permission-mode" in captured["argv"], persona
        assert "acceptEdits" in captured["argv"], persona


def test_persona_permission_config_override(monkeypatch, tmp_path):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        raise FileNotFoundError("claude not found")

    monkeypatch.setattr("subprocess.run", fake_run)
    adapter = ClaudeCodeAdapter(
        workdir=tmp_path,
        persona_permissions={"validator": "edit", "developer": "text"},
    )
    adapter.execute("validator", "ctx", make_task())
    assert "--permission-mode" in captured["argv"]
    adapter.execute("developer", "ctx", make_task())
    assert "--permission-mode" not in captured["argv"]
