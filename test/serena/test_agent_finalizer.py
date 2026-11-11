"""
The finalizer of a :class:`SerenaAgent`, run as the garbage collector runs it, on whichever thread it interrupts: on a
thread that holds a logging handler's lock, it returns, since it takes no lock on that thread; on an agent whose
construction did not finish, and at interpreter shutdown, when no thread can start, it returns without raising.
"""

import logging
import threading

import pytest

from serena.agent import SerenaAgent
from serena.config.serena_config import SerenaConfig


class _FinalizingHandler(logging.Handler):
    """A handler whose lock is not reentrant and which finalizes an agent while holding it."""

    def __init__(self, agent: SerenaAgent) -> None:
        super().__init__(level=logging.INFO)
        self._agent = agent

    def createLock(self) -> None:
        self.lock = threading.Lock()  # type: ignore[assignment]

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage() == "finalize":
            self._agent.__del__()


def _agent() -> SerenaAgent:
    return SerenaAgent(serena_config=SerenaConfig(gui_log_window=False, web_dashboard=False))


def test_an_agent_finalized_on_a_thread_holding_a_logging_handler_lock_returns() -> None:
    agent = _agent()
    handler = _FinalizingHandler(agent)
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        finalizing = threading.Thread(target=lambda: logging.getLogger(__name__).info("finalize"), daemon=True)
        finalizing.start()
        finalizing.join(10)

        assert not finalizing.is_alive(), "the finalizer did not return within 10 s"
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


def test_an_agent_whose_construction_did_not_finish_is_finalized_without_raising() -> None:
    object.__new__(SerenaAgent).__del__()


def test_an_agent_finalized_when_no_thread_can_start_returns_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _agent()

    def refuse(_thread: threading.Thread) -> None:
        raise RuntimeError("can't create new thread at interpreter shutdown")

    monkeypatch.setattr(threading.Thread, "start", refuse)

    agent.__del__()
