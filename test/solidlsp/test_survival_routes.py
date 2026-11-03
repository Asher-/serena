"""
The routes of the kept processes' take-over no other test takes, against the real brain-supervise program named by
BRAIN_SUPERVISE: a held process whose record is gone or that a first use takes, a state a rival removes or whose record
is gone, the notifications a client sends a server it started or adopted, and a bridge whose FIFO is gone, that will not
end on its terminate signal, or that is closed before it ever started.
"""

import json
import logging
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from solidlsp.ls_config import LanguageServerId
from solidlsp.ls_process import StdioLanguageServer, ProcessLaunchInfo
from solidlsp.ls_survival import Holdings, LanguageServerState, LanguageServerStates
from solidlsp.structural.bridge_process import BridgeState, BridgeStates, StructuralBridge
from solidlsp.util import supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")

SERVER = textwrap.dedent(
    """
    import json, sys
    journal = open(sys.argv[1], "a", buffering=1)
    def read():
        length = 0
        while True:
            line = sys.stdin.buffer.readline()
            if not line.strip():
                break
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":")[1])
        return json.loads(sys.stdin.buffer.read(length))
    while True:
        message = read()
        journal.write(json.dumps(message) + "\\n")
        if "id" in message:
            body = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {"capabilities": {}}}).encode()
            sys.stdout.buffer.write(b"Content-Length: %d\\r\\n\\r\\n" % len(body) + body)
            sys.stdout.buffer.flush()
    """
)

BRIDGE = textwrap.dedent(
    """
    import json, os, signal, struct
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        (length,) = struct.unpack("<I", os.read(0, 4))
        request = json.loads(os.read(0, length))
        encoded = json.dumps({"value": request.get("value", request["op"]), "pid": os.getpid()}).encode()
        os.write(1, struct.pack("<I", len(encoded)) + encoded)
    """
)


@pytest.fixture(autouse=True)
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The records' root, whose processes are killed after the test whatever it left running."""
    directory = tmp_path / "restart"
    monkeypatch.setenv(supervise.ROOT_ENV, str(directory))
    yield directory
    for path in (directory / supervise.OWNER / "process").glob("*.json"):
        if "ended_at" not in json.loads(path.read_text()):
            subprocess.run([supervise.program(), "stop", "-signal", "SIGKILL", str(path)], check=False, stderr=subprocess.DEVNULL)


def _in_frame(thread: threading.Thread, function: str, line: int | None = None) -> bool:
    """:return: whether the thread is observed running the function, at the line when one is named."""
    frame = sys._current_frames().get(thread.ident or 0)
    while frame is not None:
        if frame.f_code.co_name == function and (line is None or frame.f_lineno == line):
            return True
        frame = frame.f_back
    return False


def _await(condition: Any, what: str) -> None:
    deadline = time.monotonic() + 60
    while not condition():
        assert time.monotonic() < deadline, what
        time.sleep(0.001)


def _watcher(state: LanguageServerState) -> threading.Thread:
    [thread] = [thread for thread in threading.enumerate() if thread.name == f"held-{Path(state.record).stem}"]
    return thread


def _held(tmp_path: Path) -> tuple[LanguageServerStates, LanguageServerState, supervise.Popen]:
    """:return: the states of a command and the state of a live process of it, held by this instance."""
    states = LanguageServerStates(["held"], str(tmp_path))
    process = supervise.Popen(["/bin/sleep", "300"], call_id="", keep=True)
    state = LanguageServerState(record=str(process.record), instance_pid=0)
    state.hold()
    states.save(state)
    return states, state, process


def test_a_held_process_whose_record_is_gone_is_released_by_its_watcher_and_its_state_removed(tmp_path: Path) -> None:
    states = LanguageServerStates(["gone"], str(tmp_path))
    state = LanguageServerState(record=str(tmp_path / "absent.json"), instance_pid=0)
    states.save(state)
    holdings = Holdings()

    holdings.hold(states, state)
    _await(lambda: not list(states.kind_directory().rglob("*.json")), "the watcher did not remove the state")

    assert holdings.take(states) is None


def test_a_held_process_a_first_use_takes_is_left_by_its_watcher(tmp_path: Path) -> None:
    states, state, process = _held(tmp_path)
    holdings = Holdings()
    holdings.hold(states, state)

    # the watcher, found while it runs: once the process is taken it can end at any moment
    watcher = _watcher(state)
    assert holdings.take(states) is state
    watcher.join(60)

    assert process.poll() is None
    assert [path.name for path in states.kind_directory().rglob("*.json")] == [f"{process.record.stem}.json"]
    process.kill()
    process.wait()


def test_a_held_process_whose_record_is_discarded_is_taken_by_no_first_use_and_its_watcher_leaves_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    states, state, process = _held(tmp_path)
    holdings = Holdings()
    monkeypatch.setattr(supervise, "WATCH_SECONDS", 2.0)
    holdings.hold(states, state)
    watcher = _watcher(state)
    _await(lambda: _in_frame(watcher, "_watch", 260), "the watcher did not wait on the process")

    # the record discarded while the watcher waits, and the first use finding nothing to take
    process.release()
    assert holdings.take(states) is None
    watcher.join(60)

    assert not watcher.is_alive()
    assert list(states.kind_directory().rglob("*.json")) == []
    os.kill(process.pid, 9)


def test_a_held_process_that_ended_before_its_watcher_looked_again_is_taken_by_no_first_use_and_its_record_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    states, state, process = _held(tmp_path)
    holdings = Holdings()
    monkeypatch.setattr(supervise, "WATCH_SECONDS", 60.0)
    holdings.hold(states, state)
    _await(lambda: _in_frame(_watcher(state), "_watch", 260), "the watcher did not wait on the process")

    process.kill()
    process.wait()

    assert holdings.take(states) is None
    assert not process.record.exists()
    assert list(states.kind_directory().rglob("*.json")) == []


def test_a_state_a_rival_removes_before_this_instance_claims_it_is_not_claimed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    states = LanguageServerStates(["rival"], str(tmp_path))
    state = LanguageServerState(record=str(tmp_path / "20261031T000000-a.json"), instance_pid=1, instance_start="1.000000")
    states.save(state)
    real_alive = supervise.alive

    # the rival removes the state between this instance reading it and renaming it; every other process is as it is
    def rival_removes_first(pid: int, start: str) -> bool:
        if pid == 1:
            LanguageServerStates(["rival"], str(tmp_path)).remove(state)
            return False
        return real_alive(pid, start)

    monkeypatch.setattr(supervise, "alive", rival_removes_first)

    assert states.claim() is None


def test_a_state_whose_record_is_gone_is_removed_and_nothing_is_taken_over(tmp_path: Path) -> None:
    states = LanguageServerStates(["recordless"], str(tmp_path))
    states.save(LanguageServerState(record=str(tmp_path / "absent.json"), instance_pid=1, instance_start="1.000000"))
    (states.kind_directory() / "stray").write_text("not a command directory")

    LanguageServerStates.hold_everywhere()

    assert [path.name for path in states.kind_directory().rglob("*.json")] == []


def test_a_client_keeps_the_documents_a_server_holds_as_it_opens_changes_and_closes_them(tmp_path: Path) -> None:
    server = tmp_path / "server.py"
    server.write_text(SERVER)
    journal = tmp_path / "journal"
    command = [sys.executable, str(server), str(journal)]
    client = StdioLanguageServer(ProcessLaunchInfo(cmd=command, cwd=str(tmp_path)), LanguageServerId.PYTHON, lambda _: logging.INFO)
    client._mark_consumed(10)
    client.start()
    client.send_request("initialize", {})
    client.send_notification("initialized", {})
    client.send_notification("$/ping", None)
    for uri in ("file:///a", "file:///b", "file:///c"):
        client.send_notification("textDocument/didOpen", {"textDocument": {"uri": uri, "version": 1, "languageId": "python", "text": "x"}})
    client.send_notification(
        "textDocument/didChange", {"textDocument": {"uri": "file:///a", "version": 2}, "contentChanges": [{"text": "y"}]}
    )
    part = {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}, "text": "z"}
    client.send_notification("textDocument/didChange", {"textDocument": {"uri": "file:///b", "version": 2}, "contentChanges": [part]})
    client.send_notification("textDocument/didClose", {"textDocument": {"uri": "file:///c"}})
    client.send_notification("workspace/didChangeConfiguration", {"settings": {}})
    client.send_request("shutdown", None)

    [state_file] = LanguageServerStates.kind_directory().rglob("*.json")
    documents = json.loads(state_file.read_text())["open_documents"]
    assert sorted(documents) == ["file:///a", "file:///b"]
    assert documents["file:///a"]["version"] == 2 and documents["file:///a"]["digest"] is not None
    assert documents["file:///b"] == {"version": 2, "digest": None}
    methods = [json.loads(line).get("method") for line in journal.read_text().splitlines()]
    assert methods == ["initialize", "initialized", "$/ping"] + ["textDocument/didOpen"] * 3 + [
        "textDocument/didChange",
        "textDocument/didChange",
        "textDocument/didClose",
        "workspace/didChangeConfiguration",
        "shutdown",
    ]
    client.stop()


def test_a_bridge_whose_fifo_is_gone_is_replaced_and_one_deaf_to_its_terminate_signal_is_killed(tmp_path: Path) -> None:
    script = tmp_path / "deaf.py"
    script.write_text(BRIDGE)
    command = [sys.executable, str(script)]
    never_started = StructuralBridge(command, "deaf")
    never_started.close()
    first = StructuralBridge(command, "deaf")
    pid = first.call("echo", value="one")["pid"]

    # the first bridge left as an ended instance leaves it, its FIFO gone
    assert first._state is not None and first._process is not None
    first._state.instance_pid, first._state.instance_start = 1, "1.000000"
    BridgeStates(command).save(first._state)
    first._process.record.with_suffix(".in").unlink()
    left = first._process
    first._process, first._state = None, None

    second = StructuralBridge(command, "deaf")
    assert second.call("echo", value="two")["pid"] != pid
    second.close()

    assert left.poll() is None
    left.kill()
    left.wait()
    assert list(BridgeStates(command).kind_directory().rglob("*.json")) == []
    assert BridgeState is not None


def test_an_adopted_server_that_ends_before_its_fifo_is_read_is_reported_as_ended(tmp_path: Path) -> None:
    # a live server an ended instance held, which has closed its stdin and so leaves its FIFO no reader
    go = tmp_path / "go"
    command = ["/bin/sh", "-c", f'read line; exec 0<&-; echo closed; while [ ! -e "{go}" ]; do sleep 0.01; done; exit 3']
    server = supervise.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        call_id="",
        waiting=supervise.Waiting.of("solidlsp.language_server"),
        keep=True,
    )
    assert server.stdin is not None and server.stdout is not None
    server.stdin.write(b"written\n")
    server.stdin.flush()
    assert server.stdout.readline() == b"closed\n"
    server.close_streams()
    states = LanguageServerStates(command, str(tmp_path))
    states.save(LanguageServerState(record=str(server.record), instance_pid=1, instance_start="1.000000", initialize_result={}))

    client = StdioLanguageServer(ProcessLaunchInfo(cmd=command, cwd=str(tmp_path)), LanguageServerId.PYTHON, lambda _: logging.INFO)
    raised: list[BaseException] = []

    def start() -> None:
        try:
            client.start()
        except RuntimeError as e:
            raised.append(e)

    starter = threading.Thread(target=start)
    starter.start()
    _await(lambda: _in_frame(starter, "_open_fifo"), "the client did not wait on the server's FIFO")
    go.touch()
    starter.join(60)

    assert len(raised) == 1 and "terminated immediately with code 3" in str(raised[0])
