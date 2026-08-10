import time

from ai_sdlc.state.session import SessionState
from ai_sdlc.workspace import Workspace


def test_session_roundtrip_and_clear(tmp_workspace):
    Workspace.init(tmp_workspace)
    ws = Workspace(tmp_workspace)

    assert SessionState.load(ws) is None

    state = SessionState(run_id="20260809-101010-abc123", started_at=time.time())
    state.branch = "feature/demo"
    state.attempts["T1"] = 2
    state.last_error["T1"] = "compile failed"
    state.snapshot = {"a.py": [1, 2]}
    state.save(ws)

    assert ws.session_path.is_file()
    loaded = SessionState.load(ws)
    assert loaded.run_id == "20260809-101010-abc123"
    assert loaded.branch == "feature/demo"
    assert loaded.attempts["T1"] == 2
    assert loaded.last_error["T1"] == "compile failed"
    assert loaded.snapshot == {"a.py": [1, 2]}

    assert SessionState.clear(ws) is True
    assert SessionState.load(ws) is None
    assert SessionState.clear(ws) is False
