"""
The agent's shutdown frees its resources and signals its process to end, even when freeing them fails.
"""

import errno
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


def test_an_agent_ends_its_process_on_descriptor_exhaustion_until_it_has_shut_down(
    agent: SerenaAgent, signals: list[tuple[int, int]]
) -> None:
    """
    Descriptor exhaustion reported while the agent is live ends its process through the agent's shutdown; once the agent
    has shut down, a further exhaustion ends nothing through it.
    """
    from solidlsp.util.descriptor_exhaustion import DescriptorExhaustion

    DescriptorExhaustion.report(OSError(errno.EMFILE, "Too many open files"), "a site")
    DescriptorExhaustion.report(OSError(errno.EMFILE, "Too many open files"), "a site")

    assert signals == [(os.getpid(), signal.SIGTERM)]


def test_descriptor_exhaustion_reported_after_the_agent_shut_down_ends_nothing(
    agent: SerenaAgent, signals: list[tuple[int, int]]
) -> None:
    """Descriptor exhaustion reported once the agent has shut down ends nothing through it."""
    from solidlsp.util.descriptor_exhaustion import DescriptorExhaustion

    SerenaAgent.on_shutdown(agent, timeout=0.5)
    DescriptorExhaustion.report(OSError(errno.EMFILE, "Too many open files"), "a site")

    assert signals == []
