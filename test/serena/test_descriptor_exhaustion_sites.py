"""
Descriptor exhaustion the daemon meets outside a tool's body ends the daemon through its agent's shutdown.
"""

import errno
import gc
import io
import json
import logging
import os
import resource
import signal
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from serena.agent import SerenaAgent
from serena.call_journal import CallJournal, CallResult
from serena.config.serena_config import SerenaConfig
from serena.ls_manager import LanguageServerManager
from solidlsp.ls_config import LanguageServerId
from solidlsp.ls_process import StdioLanguageServer
from solidlsp.util import supervise


@dataclass
class _Ending:
    """What ending the daemon did: the shutdowns its agent ran and the signals its process sent."""

    shutdowns: int = 0
    signals: list[tuple[int, int]] = field(default_factory=list)


@pytest.fixture(autouse=True)
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The restart root, holding this test's journal and process records only."""
    monkeypatch.setenv(supervise.ROOT_ENV, str(tmp_path))
    return tmp_path


@pytest.fixture
def ending(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Ending]:
    """
    The ending of a daemon whose agent is live for the test: the agent's real shutdown runs, and the signal it sends the
    process is recorded rather than delivered.
    """
    agent = SerenaAgent(serena_config=SerenaConfig(gui_log_window=False, web_dashboard=False))
    observed = _Ending()
    real_shutdown = agent.shutdown

    def shutdown() -> None:
        observed.shutdowns += 1
        real_shutdown()

    monkeypatch.setattr(agent, "shutdown", shutdown)
    monkeypatch.setattr(os, "kill", lambda process_id, signal_number: observed.signals.append((process_id, signal_number)))
    yield observed
    agent.on_shutdown(timeout=0.5)


@contextmanager
def _descriptors_exhausted() -> Iterator[None]:
    """
    The process's descriptor table held full: the soft limit is lowered to just above the lowest free descriptor and every
    descriptor below it is held open, so the next descriptor the process opens fails with EMFILE. The collector is paused,
    so that no finalizer frees a descriptor while the block runs; all of it is restored when the block ends.
    """
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    gc.collect()
    gc.disable()
    held: list[int] = []
    try:
        lowest_free = os.open(os.devnull, os.O_RDONLY)
        held.append(lowest_free)
        resource.setrlimit(resource.RLIMIT_NOFILE, (lowest_free + 1, hard))
        try:
            while True:
                held.append(os.open(os.devnull, os.O_RDONLY))
        except OSError as e:
            assert e.errno == errno.EMFILE, e
        yield
    finally:
        for descriptor in held:
            os.close(descriptor)
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
        gc.enable()


def _assert_ended(ending: _Ending) -> None:
    assert ending.shutdowns == 1
    assert ending.signals == [(os.getpid(), signal.SIGTERM)]


def test_a_call_the_journal_receives_with_no_descriptor_left_ends_the_daemon(ending: _Ending) -> None:
    """A call whose journal entry cannot be written for want of file descriptors fails, and the daemon is ended."""
    journal = CallJournal()

    with _descriptors_exhausted(), pytest.raises(OSError) as raised:
        journal.receive("call-1", "probe", {})
    journal.close()

    assert raised.value.errno == errno.EMFILE
    _assert_ended(ending)


def test_a_call_the_journal_finishes_with_no_descriptor_left_ends_the_daemon(ending: _Ending) -> None:
    """A call whose answer cannot be journaled for want of file descriptors fails, and the daemon is ended."""
    journal = CallJournal()
    journal.receive("call-1", "probe", {})

    with _descriptors_exhausted(), pytest.raises(OSError) as raised:
        journal.finish("call-1", CallResult("found"))
    journal.close()

    assert raised.value.errno == errno.EMFILE
    _assert_ended(ending)


def test_a_call_the_journal_resumes_with_no_descriptor_left_ends_the_daemon(ending: _Ending) -> None:
    """A call whose journal entry cannot be read to resume it for want of file descriptors fails, and the daemon is ended."""
    journal = CallJournal()
    journal.receive("call-1", "probe", {})
    journal.finish("call-1", CallResult("found"))

    with _descriptors_exhausted(), pytest.raises(OSError) as raised:
        journal.resume("call-1")
    journal.close()

    assert raised.value.errno == errno.EMFILE
    _assert_ended(ending)


def test_a_call_the_journal_cannot_record_for_another_os_error_leaves_the_daemon_running(ending: _Ending, root: Path) -> None:
    """A call whose journal entry cannot be written for a reason other than descriptor exhaustion does not end the daemon."""
    journal = CallJournal()
    (root / supervise.OWNER / "entry").rmdir()

    with pytest.raises(OSError) as raised:
        journal.receive("call-1", "probe", {})
    journal.close()

    assert raised.value.errno == errno.ENOENT
    assert ending.shutdowns == 0
    assert ending.signals == []


def test_a_record_discarded_with_no_descriptor_left_ends_the_daemon(ending: _Ending, root: Path) -> None:
    """A process record whose removal from the live index cannot be written for want of file descriptors ends the daemon."""
    process = root / supervise.OWNER / "process"
    process.mkdir(parents=True, exist_ok=True)
    (process / "discarded.json").write_text(json.dumps({"owner": supervise.OWNER, "id": "discarded"}))
    live = root / supervise.OWNER / "live" / "process.json"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text(json.dumps(["discarded"]))
    record = supervise.Record.load(process / "discarded.json")

    with _descriptors_exhausted(), pytest.raises(OSError) as raised:
        record.discard()

    assert raised.value.errno == errno.EMFILE
    _assert_ended(ending)


def test_a_language_server_stderr_reader_failing_for_want_of_descriptors_ends_the_daemon(ending: _Ending) -> None:
    """A language server's stderr reader that fails for want of file descriptors ends the daemon."""
    server = StdioLanguageServer(
        process_launch_info=MagicMock(),
        ls_id=LanguageServerId.CPP,
        determine_log_level=lambda _line: logging.WARNING,
    )
    process = MagicMock()
    process.stderr = io.BytesIO(b"")
    process.poll.side_effect = OSError(errno.EMFILE, "Too many open files")
    server._process = process
    server._is_stopping = True

    server._read_ls_process_stderr()

    _assert_ended(ending)


def test_a_language_server_failing_to_start_for_want_of_descriptors_ends_the_daemon(ending: _Ending) -> None:
    """A language server whose start fails for want of file descriptors ends the daemon."""
    language_server = MagicMock()
    language_server.start.side_effect = OSError(errno.EMFILE, "Too many open files")
    factory = MagicMock()
    factory.create_language_server.return_value = language_server

    LanguageServerManager.from_languages([LanguageServerId.CPP], factory, MagicMock())

    _assert_ended(ending)
