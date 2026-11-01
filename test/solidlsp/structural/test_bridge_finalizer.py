"""
A bridge that is garbage collected stops the process it started, which no signal that ends serena reaches.
"""

import pytest

from solidlsp.structural.bridge_process import StructuralBridge


def test_a_collected_bridge_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    closes: list[int] = []
    monkeypatch.setattr(StructuralBridge, "close", lambda _self: closes.append(1))
    bridge = StructuralBridge.__new__(StructuralBridge)

    bridge.__del__()

    assert closes == [1]
