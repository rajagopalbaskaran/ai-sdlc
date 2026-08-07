import pytest


@pytest.fixture
def tmp_workspace(tmp_path):
    """An empty target-project workspace directory."""
    root = tmp_path / "demo-app"
    root.mkdir()
    return root
