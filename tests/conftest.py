from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_sigil_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point Sigil state and session dirs at a temp dir for every test.

    Without this, helpers like `discussion_turns()` read the developer's real
    `~/.sigil` state, so tests pass only on machines with no recorded history.
    """
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("SIGIL_SESSION_DIR", raising=False)
    monkeypatch.delenv("SIGIL_SESSION_ID", raising=False)
