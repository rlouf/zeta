from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from sigil.ledger import close_ledger_indexes


@pytest.fixture(autouse=True)
def isolate_sigil_state(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Point Sigil state and session dirs at a temp dir for every test.

    Without this, helpers like `recent_turns()` and the zeta trace store read
    the developer's real `~/.sigil` state, so tests pass only on machines with
    no recorded history.
    The same applies to `HOME`: project context, skills, tool plugins, and
    model profiles are discovered under `~/.zeta` and `~/.agents`.
    """
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("SIGIL_SESSION_DIR", raising=False)
    monkeypatch.delenv("SIGIL_SESSION_ID", raising=False)
    yield
    close_ledger_indexes()
