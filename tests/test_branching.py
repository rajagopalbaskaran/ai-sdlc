import subprocess

import pytest

from ai_sdlc.governance.branching import (
    checkout_branch,
    current_branch,
    has_remote,
    push_branch,
)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.local"], cwd=root, check=True)
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=root, check=True)
    return root


def test_checkout_creates_and_switches(repo):
    assert current_branch(repo) == "main"
    assert checkout_branch(repo, "feature/x")
    assert current_branch(repo) == "feature/x"
    # switching back and forth to an existing branch also works
    assert checkout_branch(repo, "main")
    assert checkout_branch(repo, "feature/x")
    assert current_branch(repo) == "feature/x"


def test_has_remote(repo, tmp_path):
    assert not has_remote(repo)
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=repo, check=True)
    assert has_remote(repo)


def test_push_branch_to_local_bare_remote(repo, tmp_path):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=repo, check=True)
    checkout_branch(repo, "feature/x")
    ok, _ = push_branch(repo, "feature/x")
    assert ok
    listed = subprocess.run(
        ["git", "--git-dir", str(bare), "branch", "--list"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "feature/x" in listed


def test_push_without_remote_fails_cleanly(repo):
    ok, message = push_branch(repo, "main")
    assert not ok
    assert message
