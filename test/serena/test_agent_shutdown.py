"""
The agent's shutdown frees its resources and signals its process to end, even when freeing them fails.
"""

import os
import signal
from collections.abc import Iterator

import pytest

from serena.agent import SerenaAgent
from serena.config.serena_config import SerenaConfig


@pytest.fixture
def agent() -> Iterator[SerenaAgent]:
    a = SerenaAgent(serena_config=SerenaConfig(gui_log_window=False, web_dashboard=False))
    yield a
    SerenaAgent.on_shutdown(a, timeout=0.5)


@pytest.fixture
def signals(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """The signals the process sends, recorded rather than delivered."""
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda process_id, signal_number: sent.append((process_id, signal_number)))
    return sent


def test_shutdown_tears_the_agent_down_and_signals_the_process_to_end(agent: SerenaAgent, signals: list[tuple[int, int]]) -> None:
    agent.shutdown()

    assert signals == [(os.getpid(), signal.SIGTERM)]


def test_shutdown_signals_the_process_to_end_even_when_teardown_raises(
    agent: SerenaAgent, signals: list[tuple[int, int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_teardown(timeout: float = 2.0) -> None:
        raise RuntimeError("a project's language server would not stop")

    monkeypatch.setattr(agent, "on_shutdown", failing_teardown)

    with pytest.raises(RuntimeError):
        agent.shutdown()

    assert signals == [(os.getpid(), signal.SIGTERM)]
