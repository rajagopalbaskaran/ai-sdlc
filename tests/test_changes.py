from pathlib import Path

from ai_sdlc.changes import diff, snapshot


def test_detects_added_and_modified(tmp_path):
    (tmp_path / "a.txt").write_text("one", encoding="utf-8")
    before = snapshot(tmp_path)
    (tmp_path / "a.txt").write_text("two changed", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("new", encoding="utf-8")
    changed = diff(before, snapshot(tmp_path))
    assert {Path(p).name for p in changed} == {"a.txt", "b.txt"}


def test_detects_deletions(tmp_path):
    (tmp_path / "gone.txt").write_text("x", encoding="utf-8")
    before = snapshot(tmp_path)
    (tmp_path / "gone.txt").unlink()
    changed = diff(before, snapshot(tmp_path))
    assert {Path(p).name for p in changed} == {"gone.txt"}


def test_ignores_state_and_git_dirs(tmp_path):
    before = snapshot(tmp_path)
    (tmp_path / ".ai-sdlc" / "plan").mkdir(parents=True)
    (tmp_path / ".ai-sdlc" / "plan" / "x.md").write_text("s", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "index").write_text("s", encoding="utf-8")
    assert diff(before, snapshot(tmp_path)) == []


def test_no_change_no_diff(tmp_path):
    (tmp_path / "a.txt").write_text("same", encoding="utf-8")
    assert diff(snapshot(tmp_path), snapshot(tmp_path)) == []
