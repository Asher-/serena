"""
Descriptor exhaustion the daemon meets outside a tool's body ends the daemon through its agent's shutdown.
"""

import errno
import io
import json
import logging
import os
import resource
import signal
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from serena.agent import SerenaAgent
from serena.call_journal import CallJournal, CallResult
from serena.config.serena_config import SerenaConfig
from serena.cursor import CursorManager
from serena.ls_manager import LanguageServerManager
from serena.lsp.lsp_diagnostics import PublishedDiagnosticsSnapshot
from serena.symbol_extent import compute_widened_body_text
from serena.tools.cursor_tools import CursorRenameTool, _reply_after_landed_edit
from serena.util import staleness
from serena.util.file_lifecycle import FilesystemLifecycle
from serena.util.file_system import walk_dir_entries
from serena.util.text_utils import LineType, MatchedConsecutiveLines, MultiFileReplacement, TextLine
from solidlsp.language_servers import scala_language_server
from solidlsp.language_servers.pascal_server import PascalLanguageServer
from solidlsp.language_servers.scala_language_server import _contains_json_file, _is_build_root
from solidlsp.ls_config import LanguageServerId
from solidlsp.ls_process import StdioLanguageServer, TCPConnectionInfo, TCPLanguageServer
from solidlsp.structural.bridge_process import StructuralBridge
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
    The process out of file descriptors: the soft limit is lowered to zero, so every descriptor the process opens fails
    with EMFILE, whichever descriptors another thread frees meanwhile; the descriptors already open stay usable. The limit
    is restored when the block ends.
    """
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (0, hard))
    try:
        yield
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


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


def _exhausted(*_arguments: Any) -> None:
    """An operation that fails for want of file descriptors."""
    raise OSError(errno.EMFILE, "Too many open files")


def _trace_failing_for_want_of_descriptors(*_arguments: Any) -> None:
    """A trace of language-server messages that fails with an error its own failure to open a file caused."""
    try:
        _exhausted()
    except OSError as e:
        raise RuntimeError("the trace could not be written") from e


def _stdio_server(logger: Callable[[str, str, Any], None] | None = None) -> StdioLanguageServer:
    """
    :param logger: the trace of the server's messages.
    :return: a stdio language server that runs no process.
    """
    return StdioLanguageServer(
        process_launch_info=MagicMock(),
        ls_id=LanguageServerId.CPP,
        determine_log_level=lambda _line: logging.WARNING,
        logger=logger,
    )


def _tcp_server(reader: MagicMock, logger: Callable[[str, str, Any], None] | None = None) -> TCPLanguageServer:
    """
    :param reader: the stand-in for the reader of the server's socket.
    :param logger: the trace of the server's messages.
    :return: a TCP language server connected through a stand-in socket.
    """
    server = TCPLanguageServer(TCPConnectionInfo(), LanguageServerId.CPP, lambda _line: logging.WARNING, logger)
    server._sock = MagicMock()
    server._file = reader
    return server


_HEADER = b"Content-Length: 2\r\n"
"""the header of a language-server message whose body is two bytes long."""


def test_a_language_server_stdout_reader_failing_for_want_of_descriptors_ends_the_daemon(ending: _Ending) -> None:
    """A language server's stdout reader that fails for want of file descriptors ends the daemon."""
    server = _stdio_server()
    process = MagicMock()
    process.stdout = io.BytesIO(b"")
    process.poll.side_effect = OSError(errno.EMFILE, "Too many open files")
    server._process = process
    server._is_stopping = True

    server._read_ls_process_stdout()

    _assert_ended(ending)


def test_a_language_server_message_whose_trace_fails_for_want_of_descriptors_ends_the_daemon(ending: _Ending) -> None:
    """A message from a language server whose trace cannot be written for want of file descriptors ends the daemon."""
    server = _stdio_server(logger=_exhausted)

    server._handle_body(b'{"method": "window/logMessage"}')

    _assert_ended(ending)


def test_a_language_server_response_whose_delivery_fails_for_want_of_descriptors_ends_the_daemon(ending: _Ending) -> None:
    """A response from a language server that cannot be delivered to its request for want of file descriptors ends the daemon."""
    server = _stdio_server()
    request = MagicMock()
    request.on_result.side_effect = OSError(errno.EMFILE, "Too many open files")
    server._pending_requests[1] = request

    server._handle_body(b'{"id": 1, "result": null}')

    _assert_ended(ending)


def test_a_language_server_request_whose_handler_fails_for_want_of_descriptors_ends_the_daemon(ending: _Ending) -> None:
    """A request from a language server whose handler fails for want of file descriptors ends the daemon."""
    server = _stdio_server()
    server.on_request("workspace/configuration", _exhausted)

    server._handle_body(b'{"id": 1, "method": "workspace/configuration"}')

    _assert_ended(ending)


def test_a_language_server_notification_whose_observer_fails_for_want_of_descriptors_ends_the_daemon(ending: _Ending) -> None:
    """A notification from a language server whose observer fails for want of file descriptors ends the daemon."""
    server = _stdio_server()
    server.on_any_notification(_exhausted)

    server._handle_body(b'{"method": "window/logMessage"}')

    _assert_ended(ending)


def test_a_language_server_notification_whose_handler_fails_for_want_of_descriptors_ends_the_daemon(ending: _Ending) -> None:
    """A notification from a language server whose handler fails for want of file descriptors ends the daemon."""
    server = _stdio_server()
    server.on_notification("window/logMessage", _exhausted)

    server._handle_body(b'{"method": "window/logMessage"}')

    _assert_ended(ending)


def test_a_tcp_language_server_header_read_failing_for_want_of_descriptors_ends_the_daemon(ending: _Ending) -> None:
    """A TCP language server whose message header cannot be read for want of file descriptors ends the daemon."""
    reader = MagicMock()
    reader.readline.side_effect = OSError(errno.EMFILE, "Too many open files")

    _tcp_server(reader)._read_loop()

    _assert_ended(ending)


def test_a_tcp_language_server_header_line_read_failing_for_want_of_descriptors_ends_the_daemon(ending: _Ending) -> None:
    """A TCP language server whose header lines past the first cannot be read for want of file descriptors ends the daemon."""
    reader = MagicMock()
    reader.readline.side_effect = [_HEADER, OSError(errno.EMFILE, "Too many open files"), b""]

    _tcp_server(reader)._read_loop()

    _assert_ended(ending)


def test_a_tcp_language_server_body_read_failing_for_want_of_descriptors_ends_the_daemon(ending: _Ending) -> None:
    """A TCP language server whose message body cannot be read for want of file descriptors ends the daemon."""
    reader = MagicMock()
    reader.readline.side_effect = [_HEADER, b"\r\n"]
    reader.read.side_effect = OSError(errno.EMFILE, "Too many open files")

    _tcp_server(reader)._read_loop()

    _assert_ended(ending)


def test_a_tcp_language_server_read_loop_failing_for_want_of_descriptors_ends_the_daemon(ending: _Ending) -> None:
    """A TCP language server whose read loop fails with an error that descriptor exhaustion caused ends the daemon."""
    reader = MagicMock()
    reader.readline.side_effect = [_HEADER, b"\r\n", b""]
    reader.read.return_value = b"{}"

    _tcp_server(reader, logger=_trace_failing_for_want_of_descriptors)._read_loop()

    _assert_ended(ending)


def test_a_tcp_language_server_send_failing_for_want_of_descriptors_ends_the_daemon(ending: _Ending) -> None:
    """A message to a TCP language server that cannot be sent for want of file descriptors ends the daemon."""
    server = _tcp_server(MagicMock())
    sock = MagicMock()
    sock.sendall.side_effect = OSError(errno.EMFILE, "Too many open files")
    server._sock = sock

    server.send_notification("initialized")

    _assert_ended(ending)


def test_a_stream_file_whose_prefix_cannot_be_freed_for_want_of_descriptors_ends_the_daemon(ending: _Ending, tmp_path: Path) -> None:
    """A stream file whose consumed prefix cannot be freed for want of file descriptors fails, and the daemon is ended."""
    stream = tmp_path / "stream.out"
    stream.write_bytes(b"output")

    with _descriptors_exhausted(), pytest.raises(OSError) as raised:
        supervise.reclaim(stream, 0)

    assert raised.value.errno == errno.EMFILE
    _assert_ended(ending)


def test_a_process_whose_output_cannot_be_followed_for_want_of_descriptors_ends_the_daemon(
    ending: _Ending, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process taken over whose output file cannot be opened for want of file descriptors fails, and the daemon is ended."""
    process = root / supervise.OWNER / "process"
    process.mkdir(parents=True, exist_ok=True)
    stdout = process / "adopted.out"
    stdout.write_bytes(b"")
    record = process / "adopted.json"
    record.write_text(json.dumps({"owner": supervise.OWNER, "id": "adopted", "stdout": str(stdout)}))
    real_open = os.open

    def open_failing_for_the_output(path: Any, flags: int, *arguments: Any, **keywords: Any) -> int:
        if os.fspath(path) == str(stdout):
            _exhausted()
        return real_open(path, flags, *arguments, **keywords)

    monkeypatch.setattr(os, "open", open_failing_for_the_output)

    with pytest.raises(OSError) as raised:
        supervise.Popen.adopt(record)

    assert raised.value.errno == errno.EMFILE
    _assert_ended(ending)


class _ExhaustedContinuation(supervise.Continuation):
    """A continuation whose result cannot be computed for want of file descriptors."""

    def result(self, state: Any, record: supervise.Record) -> Any:
        _exhausted()

    def failure(self, error: Exception) -> Any:
        return CallResult(str(error), True)


def test_an_adopted_call_whose_result_fails_for_want_of_descriptors_ends_the_daemon(ending: _Ending, root: Path) -> None:
    """A call left by an ended instance whose continuation fails for want of file descriptors ends the daemon."""
    process = root / supervise.OWNER / "process"
    process.mkdir(parents=True, exist_ok=True)
    (process / "ended.json").write_text(
        json.dumps(
            {
                "owner": supervise.OWNER,
                "id": "ended",
                "call_id": "call-1",
                "waiting": {"kind": "exhausted"},
                "ended_at": "2026-11-16T00:00:00Z",
                "exit_code": 0,
            }
        )
    )
    live = root / supervise.OWNER / "live" / "process.json"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text(json.dumps(["ended"]))
    continuations = supervise.Continuations()
    continuations.register("exhausted", _ExhaustedContinuation())

    supervise.Adoption(MagicMock(), (), (), continuations).adopt()

    _assert_ended(ending)


def _supervisor_failing_for_want_of_descriptors(directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Makes the brain-supervise every start runs a stand-in that reports its start failed for want of file descriptors.

    :param directory: where the stand-in is written.
    :param monkeypatch: the test's patcher, which names the stand-in as the program.
    """
    report = json.dumps({"error": "supervise: start: too many open files", "errno": errno.EMFILE})
    supervisor = directory / "brain-supervise"
    supervisor.write_text(f"#!/bin/sh\ncat >/dev/null\necho '{report}'\n")
    supervisor.chmod(0o755)
    monkeypatch.setenv(supervise.PROGRAM_ENV, str(supervisor))


def test_a_language_server_whose_supervisor_fails_for_want_of_descriptors_ends_the_daemon(
    ending: _Ending, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A language server whose supervisor fails to start it for want of file descriptors ends the daemon."""
    _supervisor_failing_for_want_of_descriptors(tmp_path, monkeypatch)
    language_server = MagicMock()
    language_server.start.side_effect = lambda: supervise.Popen([sys.executable])
    factory = MagicMock()
    factory.create_language_server.return_value = language_server

    LanguageServerManager.from_languages([LanguageServerId.CPP], factory, MagicMock())

    _assert_ended(ending)


def test_a_structural_overview_whose_bridge_fails_to_start_for_want_of_descriptors_ends_the_daemon(
    ending: _Ending, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file's structural overview whose bridge cannot be started for want of file descriptors ends the daemon."""
    _supervisor_failing_for_want_of_descriptors(tmp_path, monkeypatch)
    (tmp_path / "main.go").write_text("package main\n")
    project = MagicMock()
    project.project_root = str(tmp_path)
    project.read_file.return_value = "package main\n"
    backend = MagicMock()
    backend.walk_nodes.side_effect = lambda _tree: StructuralBridge([sys.executable], "go").call("walk_symbols")
    registry = MagicMock()
    registry.structural_backend_for.return_value = backend

    CursorManager(project, registry).structural_overview("main.go")

    _assert_ended(ending)


def test_a_cursor_whose_re_anchor_after_an_edit_fails_for_want_of_descriptors_ends_the_daemon(
    ending: _Ending, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cursor whose re-anchor after a landed edit starts a structural bridge that cannot start for want of descriptors ends the daemon."""
    _supervisor_failing_for_want_of_descriptors(tmp_path, monkeypatch)
    manager = MagicMock()
    manager.reanchor_cursor.side_effect = lambda *_arguments, **_keywords: StructuralBridge([sys.executable], "go").call("walk_symbols")

    reply = _reply_after_landed_edit(manager, "c1", "applied")

    assert reply.startswith("applied")
    _assert_ended(ending)


def test_an_adopted_bridge_whose_answer_cannot_be_read_for_want_of_descriptors_ends_the_daemon(
    ending: _Ending, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An adopted bridge whose answer cannot be read for want of file descriptors is not synchronized, and the daemon is ended."""
    bridge = StructuralBridge([sys.executable], "go")
    monkeypatch.setattr(bridge, "_receive", _exhausted)
    monkeypatch.setattr(bridge, "_send", lambda *_arguments: None)

    assert bridge._synchronize(MagicMock()) is False
    _assert_ended(ending)


def _manager_whose_retriever_fails_for_want_of_descriptors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CursorManager:
    """A cursor manager whose symbol retriever answers every lookup with descriptor exhaustion."""
    retriever = MagicMock()
    retriever.find_by_location.side_effect = _exhausted
    monkeypatch.setattr(CursorManager, "_retriever", property(lambda _self: retriever))
    project = MagicMock()
    project.project_root = str(tmp_path)
    return CursorManager(project, MagicMock())


def test_a_symbol_name_that_cannot_be_resolved_for_want_of_descriptors_ends_the_daemon(
    ending: _Ending, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symbol name lookup that fails for want of file descriptors falls back to file:line, and the daemon is ended."""
    manager = _manager_whose_retriever_fails_for_want_of_descriptors(tmp_path, monkeypatch)

    assert manager._symbol_name_at("main.go", 0, 0) == "main.go:1"
    _assert_ended(ending)


def test_a_process_start_time_that_cannot_be_read_for_want_of_descriptors_ends_the_daemon(
    ending: _Ending, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process's start time that cannot be read from /proc for want of file descriptors reads as no live process, and the daemon is ended."""
    monkeypatch.setattr(Path, "read_text", _exhausted)

    assert supervise._linux_start_time(os.getpid()) == ""
    _assert_ended(ending)


def test_a_record_that_cannot_be_taken_up_for_want_of_descriptors_ends_the_daemon(
    ending: _Ending, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process record whose take-up fails for want of file descriptors is skipped, and the daemon is ended."""
    process = root / supervise.OWNER / "process"
    process.mkdir(parents=True, exist_ok=True)
    (process / "ended.json").write_text(
        json.dumps({"owner": supervise.OWNER, "id": "ended", "ended_at": "2026-11-16T00:00:00Z", "exit_code": 0})
    )
    live = root / supervise.OWNER / "live" / "process.json"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text(json.dumps(["ended"]))
    monkeypatch.setattr(supervise.Adoption, "_take_up", _exhausted)

    supervise.Adoption(MagicMock(), (), (), supervise.Continuations()).adopt()

    _assert_ended(ending)


def test_a_cursor_whose_re_anchor_after_a_rename_fails_for_want_of_descriptors_ends_the_daemon(
    ending: _Ending, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cursor whose re-anchor after a rename starts a structural bridge that cannot start for want of descriptors ends the daemon."""
    _supervisor_failing_for_want_of_descriptors(tmp_path, monkeypatch)
    manager = MagicMock()
    manager.get_lsp_cursor.return_value.current_symbol.get_name_path.return_value = "Greeter/greet"
    manager.get_lsp_cursor.return_value.current_location.relative_path = "main.go"
    manager.reanchor_cursor.side_effect = lambda *_arguments, **_keywords: StructuralBridge([sys.executable], "go").call("walk_symbols")
    agent = MagicMock()
    agent.get_cursor_manager.return_value = manager
    editor = MagicMock()
    editor.rename_symbol.return_value = "renamed"
    tool = CursorRenameTool(agent)
    monkeypatch.setattr(tool, "create_code_editor", lambda: editor)

    reply = tool.apply("c1", "salute")

    assert reply.startswith("renamed")
    _assert_ended(ending)


def test_a_search_hit_whose_containing_symbol_cannot_be_resolved_for_want_of_descriptors_ends_the_daemon(
    ending: _Ending, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A search hit whose containing symbol cannot be resolved for want of file descriptors is surfaced unsymboled, and the daemon is ended."""
    (tmp_path / "main.go").write_text("package main\n")
    retriever = MagicMock()
    retriever.get_language_server.side_effect = _exhausted
    retriever.get_symbol_overview.return_value = {}
    monkeypatch.setattr(CursorManager, "_retriever", property(lambda _self: retriever))
    project = MagicMock()
    project.project_root = str(tmp_path)
    project.search_project_files_for_pattern.return_value = [
        MatchedConsecutiveLines(
            lines=[TextLine(line_number=0, line_content="package main", match_type=LineType.MATCH)], source_file_path="main.go"
        )
    ]

    groups, unsymboled = CursorManager(project, MagicMock()).find_pattern_with_enclosing_symbols("package", relative_path="main.go")

    assert groups == []
    assert [path for path, _hits in unsymboled] == ["main.go"]
    _assert_ended(ending)


def test_a_file_version_that_cannot_be_read_for_want_of_descriptors_ends_the_daemon(ending: _Ending, tmp_path: Path) -> None:
    """A file's version that cannot be read for want of file descriptors reads as none, and the daemon is ended."""
    (tmp_path / "main.go").write_text("package main\n")

    with _descriptors_exhausted():
        version = staleness.read_file_version(str(tmp_path), "main.go")

    assert version is None
    _assert_ended(ending)


def test_a_write_whose_target_cannot_be_read_for_want_of_descriptors_ends_the_daemon(ending: _Ending, tmp_path: Path) -> None:
    """A write whose target cannot be read for want of file descriptors is refused as stale, and the daemon is ended."""
    (tmp_path / "main.go").write_text("package main\n")

    with _descriptors_exhausted():
        stale = staleness.check_stale(str(tmp_path), "main.go", "expected")

    assert stale is not None
    _assert_ended(ending)


def test_a_file_that_cannot_be_described_for_want_of_descriptors_ends_the_daemon(ending: _Ending, tmp_path: Path) -> None:
    """A file that cannot be read for want of file descriptors is described with the error, and the daemon is ended."""
    (tmp_path / "main.go").write_text("package main\n")
    lifecycle = FilesystemLifecycle(str(tmp_path), is_ignored=lambda _path: False)

    with _descriptors_exhausted():
        described = lifecycle.stat("main.go")

    assert described.error
    _assert_ended(ending)


def test_a_directory_walk_that_cannot_scan_for_want_of_descriptors_ends_the_daemon(ending: _Ending, tmp_path: Path) -> None:
    """A directory walk that cannot scan its top directory for want of file descriptors hands on the error, and the daemon is ended."""
    errors: list[OSError] = []

    with _descriptors_exhausted():
        walked = list(walk_dir_entries(str(tmp_path), onerror=errors.append))

    assert walked == []
    assert [error.errno for error in errors] == [errno.EMFILE]
    _assert_ended(ending)


def test_a_plaintext_view_that_cannot_be_read_for_want_of_descriptors_ends_the_daemon(ending: _Ending, tmp_path: Path) -> None:
    """A file's plaintext view that cannot be read for want of file descriptors is an error view, and the daemon is ended."""
    (tmp_path / "notes.txt").write_text("notes\n")
    project = MagicMock()
    project.project_root = str(tmp_path)
    manager = CursorManager(project, MagicMock())

    with _descriptors_exhausted():
        manager._plaintext_view("notes.txt")

    _assert_ended(ending)


def test_a_scala_directory_that_cannot_be_scanned_for_want_of_descriptors_ends_the_daemon(ending: _Ending, tmp_path: Path) -> None:
    """A directory that cannot be scanned for JSON files for want of file descriptors holds none, and the daemon is ended."""
    (tmp_path / "bsp.json").write_text("{}\n")

    with _descriptors_exhausted():
        holds_json = _contains_json_file(str(tmp_path))

    assert not holds_json
    _assert_ended(ending)


def test_a_directory_that_cannot_be_listed_for_want_of_descriptors_ends_the_daemon(ending: _Ending, tmp_path: Path) -> None:
    """A directory that cannot be scanned for want of file descriptors lists no entries, and the daemon is ended."""
    (tmp_path / "main.go").write_text("package main\n")
    lifecycle = FilesystemLifecycle(str(tmp_path), is_ignored=lambda _path: False)

    with _descriptors_exhausted():
        listing = lifecycle.list_dir(".")

    assert listing.entries == ()
    _assert_ended(ending)


def test_a_delete_whose_target_cannot_be_read_for_want_of_descriptors_ends_the_daemon(ending: _Ending, tmp_path: Path) -> None:
    """A delete whose target cannot be read to check its version for want of file descriptors is refused, and the daemon is ended."""
    target = tmp_path / "main.go"
    target.write_text("package main\n")
    lifecycle = FilesystemLifecycle(str(tmp_path), is_ignored=lambda _path: False)
    version = lifecycle.stat("main.go").version
    assert version is not None

    with _descriptors_exhausted():
        result = lifecycle.delete("main.go", expect_version=version)

    assert not result.ok
    assert target.exists()
    _assert_ended(ending)


def test_a_scala_build_definition_that_cannot_be_read_for_want_of_descriptors_ends_the_daemon(
    ending: _Ending, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An sbt build definition that cannot be opened for want of file descriptors marks no build root, and the daemon is ended."""
    (tmp_path / "project").mkdir()
    (tmp_path / "project" / "build.properties").write_text("sbt.version=1.9.0\n")
    monkeypatch.setattr(scala_language_server, "open", lambda *_arguments, **_keywords: _exhausted(), raising=False)

    assert not _is_build_root(str(tmp_path))
    _assert_ended(ending)


def test_a_diagnostics_snapshot_whose_diagnostics_cannot_be_requested_for_want_of_descriptors_ends_the_daemon(ending: _Ending) -> None:
    """A diagnostics snapshot of a file whose diagnostics cannot be requested for want of file descriptors holds no warnings for it, and the daemon is ended."""
    edited = MagicMock()
    edited.after_relative_path = "main.py"
    edited.before_relative_path = "main.py"
    language_server = MagicMock()
    language_server.get_cached_published_text_document_diagnostics.return_value = None
    language_server.request_text_document_diagnostics.side_effect = lambda *_arguments, **_keywords: _exhausted()
    retriever = MagicMock()
    retriever.get_language_server.return_value = language_server

    snapshot = PublishedDiagnosticsSnapshot([edited], retriever)

    assert snapshot.warning_identities_by_before_path == {"main.py": set()}
    _assert_ended(ending)


def test_a_pascal_server_version_that_cannot_be_read_for_want_of_descriptors_ends_the_daemon(ending: _Ending, tmp_path: Path) -> None:
    """An installed Pascal server's version that cannot be read for want of file descriptors reads as none, and the daemon is ended."""
    version_file = Path(PascalLanguageServer._meta_file(str(tmp_path), "version"))
    version_file.parent.mkdir(parents=True, exist_ok=True)
    version_file.write_text("v1.0.0\n")

    with _descriptors_exhausted():
        version = PascalLanguageServer._get_local_version(str(tmp_path))

    assert version is None
    _assert_ended(ending)


def test_a_body_that_cannot_be_widened_for_want_of_descriptors_ends_the_daemon(ending: _Ending) -> None:
    """A symbol body whose file cannot be read for want of file descriptors is not widened, and the daemon is ended."""
    symbol = MagicMock()
    symbol.relative_path = "main.py"
    project = MagicMock()
    project.read_file.side_effect = _exhausted

    assert compute_widened_body_text(symbol, project) is None
    _assert_ended(ending)


def test_a_diagnostics_snapshot_whose_language_server_cannot_be_found_for_want_of_descriptors_ends_the_daemon(ending: _Ending) -> None:
    """A diagnostics snapshot of a file whose language server cannot be found for want of file descriptors omits it, and the daemon is ended."""
    edited = MagicMock()
    edited.after_relative_path = "main.py"
    edited.before_relative_path = "main.py"
    retriever = MagicMock()
    retriever.get_language_server.side_effect = _exhausted

    snapshot = PublishedDiagnosticsSnapshot([edited], retriever)

    assert snapshot.generation_by_after_path == {}
    _assert_ended(ending)


def _pascal_metadata_directory(tmp_path: Path) -> str:
    """The Pascal server directory under ``tmp_path``, its metadata directory created."""
    Path(PascalLanguageServer._meta_file(str(tmp_path), "version")).parent.mkdir(parents=True, exist_ok=True)
    return str(tmp_path)


def test_a_pascal_server_version_that_cannot_be_saved_for_want_of_descriptors_ends_the_daemon(ending: _Ending, tmp_path: Path) -> None:
    """An installed Pascal server's version that cannot be saved for want of file descriptors ends the daemon."""
    pasls_dir = _pascal_metadata_directory(tmp_path)

    with _descriptors_exhausted():
        PascalLanguageServer._save_local_version(pasls_dir, "v1.0.0")

    _assert_ended(ending)


def test_a_pascal_update_check_that_cannot_be_recorded_for_want_of_descriptors_ends_the_daemon(ending: _Ending, tmp_path: Path) -> None:
    """A Pascal server update check whose time cannot be recorded for want of file descriptors ends the daemon."""
    pasls_dir = _pascal_metadata_directory(tmp_path)

    with _descriptors_exhausted():
        PascalLanguageServer._update_last_check(pasls_dir)

    _assert_ended(ending)


def test_a_pascal_file_that_cannot_be_marked_for_cleanup_for_want_of_descriptors_ends_the_daemon(ending: _Ending, tmp_path: Path) -> None:
    """A Pascal server file that cannot be marked for cleanup for want of file descriptors ends the daemon."""
    pasls_dir = _pascal_metadata_directory(tmp_path)

    with _descriptors_exhausted():
        PascalLanguageServer._mark_for_cleanup(pasls_dir, str(tmp_path / "old"))

    _assert_ended(ending)


def test_a_pascal_cleanup_list_that_cannot_be_read_for_want_of_descriptors_ends_the_daemon(ending: _Ending, tmp_path: Path) -> None:
    """A Pascal server cleanup list that cannot be read for want of file descriptors cleans nothing up, and the daemon is ended."""
    pasls_dir = _pascal_metadata_directory(tmp_path)
    old = tmp_path / "old"
    old.write_text("old\n")
    Path(PascalLanguageServer._meta_file(pasls_dir, "cleanup_list")).write_text(f"{old}\n")

    with _descriptors_exhausted():
        PascalLanguageServer._cleanup_old_files(pasls_dir)

    assert old.exists()
    _assert_ended(ending)


def test_a_replacement_file_that_cannot_be_read_for_want_of_descriptors_ends_the_daemon(ending: _Ending) -> None:
    """A file in a replacement's scope that cannot be read for want of file descriptors is skipped, and the daemon is ended."""
    file_proxy = MagicMock()
    file_proxy.get_relative_path.return_value = "main.go"
    file_proxy.get_contents.side_effect = _exhausted
    project = MagicMock()
    project.create_file_collection.return_value.filter_glob.return_value = [file_proxy]

    assert MultiFileReplacement._collect_files(project, "", "", "") == []
    _assert_ended(ending)
