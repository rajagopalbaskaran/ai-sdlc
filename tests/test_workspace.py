import pytest

from ai_sdlc.workspace import Workspace

EXPECTED_PERSONAS = {
    "requirement_analyst",
    "implementation_planner",
    "developer",
    "validator",
    "tester",
    "deployment_engineer",
}


def test_init_creates_state_dir(tmp_workspace):
    ws = Workspace.init(tmp_workspace)
    assert (tmp_workspace / ".ai-sdlc").is_dir()
    for sub in ("knowledge-base", "plan", "personas", "runs"):
        assert (ws.state_dir / sub).is_dir()


def test_init_copies_personas_and_config(tmp_workspace):
    ws = Workspace.init(tmp_workspace)
    names = {p.stem for p in (ws.state_dir / "personas").glob("*.md")}
    assert names == EXPECTED_PERSONAS
    assert (ws.state_dir / "config.yaml").is_file()
    assert (ws.state_dir / "project-profile.md").is_file()
    assert (ws.state_dir / "plan" / "implementation-plan.md").is_file()


def test_init_refuses_overwrite(tmp_workspace):
    Workspace.init(tmp_workspace)
    with pytest.raises(FileExistsError):
        Workspace.init(tmp_workspace)


def test_workspace_exists(tmp_workspace):
    assert not Workspace(tmp_workspace).exists()
    Workspace.init(tmp_workspace)
    assert Workspace(tmp_workspace).exists()
