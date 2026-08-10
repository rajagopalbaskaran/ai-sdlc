from ai_sdlc.cli import main
from ai_sdlc.workspace import Workspace


def test_init_installs_the_slash_command(tmp_workspace):
    assert main(["init", "--workspace", str(tmp_workspace)]) == 0
    command = tmp_workspace / ".claude" / "commands" / "ai-sdlc.md"
    assert command.is_file()
    text = command.read_text(encoding="utf-8")
    assert "$ARGUMENTS" in text
    assert "ai-sdlc report-task" in text
    assert text.isascii()


def test_install_commands_is_idempotent_without_force(tmp_workspace):
    main(["init", "--workspace", str(tmp_workspace)])
    ws = Workspace(tmp_workspace)
    command = ws.commands_dir / "ai-sdlc.md"
    command.write_text("customized\n", encoding="utf-8")

    assert main(["install-commands", "--workspace", str(tmp_workspace)]) == 0
    assert command.read_text(encoding="utf-8") == "customized\n"

    assert main(["install-commands", "--force", "--workspace", str(tmp_workspace)]) == 0
    assert "$ARGUMENTS" in command.read_text(encoding="utf-8")


def test_install_commands_works_without_an_ai_sdlc_workspace(tmp_workspace):
    """Enabling the slash command must not require a project that has already
    been init'd - you often want /ai-sdlc available before running init."""
    assert not (tmp_workspace / ".ai-sdlc").exists()

    assert main(["install-commands", "--workspace", str(tmp_workspace)]) == 0
    command = tmp_workspace / ".claude" / "commands" / "ai-sdlc.md"
    assert command.is_file()
    assert "$ARGUMENTS" in command.read_text(encoding="utf-8")


def test_install_commands_user_scope_writes_to_home(tmp_workspace, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    assert main(["install-commands", "--user", "--workspace", str(tmp_workspace)]) == 0
    command = home / ".claude" / "commands" / "ai-sdlc.md"
    assert command.is_file()
    assert "$ARGUMENTS" in command.read_text(encoding="utf-8")
    # user scope must not also litter the project
    assert not (tmp_workspace / ".claude").exists()
