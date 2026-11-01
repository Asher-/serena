"""
The fixture of the tests that each function in solidlsp that runs a program starts it through brain-supervise.
"""

from pathlib import Path

import pytest

from test.solidlsp.util.supervised_stand_in import StandIns


@pytest.fixture
def stand_ins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StandIns:
    """The test's stand-ins, alone on PATH, under a restart root of the test's own."""
    return StandIns(tmp_path / "stand_ins", monkeypatch)
