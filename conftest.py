from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_zeta_state(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Point Zeta state at a temp dir for every test.

    Without the runtime override, tests could discover state from the working
    tree and read or change project records. The same isolation applies to
    `HOME`: global instructions, skills, tool plugins, and model profiles are
    discovered under `~/.zeta` and `~/.agents`.
    """
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    monkeypatch.setenv("ZETA_STATE_DIR", str(tmp_path / "state"))
    yield
