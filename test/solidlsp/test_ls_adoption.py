"""
A language server an ended instance held is taken over, by its state, only when it can be (pg://brain/daemon/restart/readoption):
the instance named by a pid the kernel gave another process holds nothing, a server that never answered initialize is
stopped rather than adopted, two servers of one command each keep a state of their own, every live server is owned from
the moment serena starts whether or not its project is ever used, and the consumed prefix of a server's stdout is freed
without moving the offset a next instance reads on from. The server is a stand-in speaking LSP; each first instance is
a process of its own, killed with its whole group as serena is.
"""

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from secrets import token_hex

import pytest

from solidlsp.ls_config import LanguageServerId
from solidlsp.ls_process import LanguageServerProcess, ProcessLaunchInfo
from solidlsp.ls_survival import HOLDINGS, LanguageServerStates
from solidlsp.util import supervise
from test.conftest import SCRATCH_ROOT

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")

SCRATCH = SCRATCH_ROOT
"""The directory under which each test here keeps its files, never a system temp directory."""

_DEADLINE_SECONDS = 60.0

SERVER = textwrap.dedent(
    """
    import json, sys
    silent = sys.argv[1] == "silent"
    def read():
        length = 0
        while True:
            line = sys.stdin.buffer.readline()
            if not line.strip():
                break
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":")[1])
        return json.loads(sys.stdin.buffer.read(length))
    def answer(request_id, result):
        body = json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}).encode()
        sys.stdout.buffer.write(b"Content-Length: %d\\r\\n\\r\\n" % len(body) + body)
        sys.stdout.buffer.flush()
    while True:
        message = read()
        method = message.get("method")
        if method == "initialize":
            if not silent:
                answer(message["id"], {"capabilities": {}})
        elif method == "big":
            answer(message["id"], "x" * message["params"]["size"])
        elif "id" in message:
            answer(message["id"], method)
    """
)

FIRST_INSTANCE = textwrap.dedent(
    """
    import json, logging, sys, threading, time
    from solidlsp.ls_config import LanguageServerId
    from solidlsp.ls_process import LanguageServerProcess, ProcessLaunchInfo
    command, directory, servers, big = json.loads(sys.argv[1]), sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
    started = []
    for _ in range(servers):
        process = LanguageServerProcess(ProcessLaunchInfo(cmd=command, cwd=directory), LanguageServerId.PYTHON, lambda _: logging.INFO)
        process.start()
        started.append(process)
        if "silent" in command:
            threading.Thread(target=process.send_request, args=("initialize", {}), daemon=True).start()
            time.sleep(0.5)
        else:
            process.send_request("initialize", {})
        for _ in range(big):
            assert len(process.send_request("big", {"size": 600_000})) == 600_000
    print(json.dumps([process.process.pid for process in started]), flush=True)
    time.sleep(300)
    """
)


@pytest.fixture
def scratch(request: pytest.FixtureRequest) -> Iterator[Path]:
    directory = SCRATCH / f"{request.node.name[:48]}-{token_hex(4)}"
    directory.mkdir(parents=True)
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def root(scratch: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The instances' root, whose processes are killed after the test whatever it left running."""
    directory = scratch / "restart"
    monkeypatch.setenv(supervise.ROOT_ENV, str(directory))
    yield directory
    for path in (directory / supervise.OWNER / "process").glob("*.json"):
        if "ended_at" not in json.loads(path.read_text()):
            subprocess.run([supervise.program(), "stop", "-signal", "SIGKILL", str(path)], check=False, stderr=subprocess.DEVNULL)


def _command(scratch: Path, mode: str = "answers") -> list[str]:
    server = scratch / "server.py"
    server.write_text(SERVER)
    return [sys.executable, str(server), mode]


def _first_instance(
    scratch: Path, root: Path, command: list[str], servers: int = 1, big: int = 0, settled: Callable[[], bool] = lambda: True
) -> list[int]:
    """
    Runs a first instance that starts the servers, and kills its whole group once it reports them and ``settled`` holds.

    :return: the servers' pids.
    """
    instance = subprocess.Popen(
        [sys.executable, "-c", FIRST_INSTANCE, json.dumps(command), str(scratch), str(servers), str(big)],
        env={**os.environ, supervise.ROOT_ENV: str(root)},
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert instance.stdout is not None
    pids = json.loads(instance.stdout.readline())
    _await(settled, "the first instance did not settle")
    os.killpg(instance.pid, signal.SIGKILL)
    instance.wait()
    return pids


def _client(command: list[str], scratch: Path) -> LanguageServerProcess:
    return LanguageServerProcess(ProcessLaunchInfo(cmd=command, cwd=str(scratch)), LanguageServerId.PYTHON, lambda _: logging.INFO)


def _state_files(root: Path) -> list[Path]:
    return sorted(LanguageServerStates.kind_directory().rglob("*.json"))


def _await(condition: object, what: str) -> None:
    deadline = time.monotonic() + _DEADLINE_SECONDS
    while not condition():  # type: ignore[operator]
        assert time.monotonic() < deadline, what
        time.sleep(0.01)


def test_a_server_whose_instance_pid_the_kernel_gave_another_process_is_adopted(scratch: Path, root: Path) -> None:
    command = _command(scratch)
    [pid] = _first_instance(scratch, root, command)

    # the ended instance's pid, now a live process's that started at another time
    [path] = _state_files(root)
    fields = json.loads(path.read_text())
    path.write_text(json.dumps({**fields, "instance_pid": os.getppid()}))

    client = _client(command, scratch)
    client.start()

    assert client.process is not None and client.process.pid == pid
    client.stop()


def test_a_server_that_never_answered_initialize_is_stopped_and_not_adopted(scratch: Path, root: Path) -> None:
    command = _command(scratch, "silent")
    [pid] = _first_instance(scratch, root, command)

    client = _client(command, scratch)
    client.start()

    assert client.process is not None and client.process.pid != pid
    assert not supervise.start_time(pid)
    assert [json.loads(path.read_text())["record"] for path in _state_files(root)] == [str(client.process.record)]
    client.stop()


def test_two_servers_of_one_command_each_keep_their_state_and_are_each_adopted(scratch: Path, root: Path) -> None:
    command = _command(scratch)
    pids = _first_instance(scratch, root, command, servers=2)

    clients = [_client(command, scratch) for _ in range(2)]
    for client in clients:
        client.start()

    assert sorted(client.process.pid for client in clients if client.process is not None) == sorted(pids)
    for client in clients:
        client.stop()


def test_every_live_server_is_owned_from_startup_and_one_that_ends_unused_has_its_outcome_consumed(scratch: Path, root: Path) -> None:
    command = _command(scratch)
    [pid] = _first_instance(scratch, root, command)
    [path] = _state_files(root)
    record = Path(json.loads(path.read_text())["record"])

    LanguageServerStates.hold_everywhere()

    assert json.loads(path.read_text())["instance_pid"] == os.getpid()
    subprocess.run([supervise.program(), "stop", "-signal", "SIGTERM", str(record)], check=True)
    _await(lambda: not path.exists() and not record.exists(), "the ended server's state and record were not consumed")
    assert not supervise.start_time(pid)


def test_the_first_use_of_a_command_attaches_to_the_server_held_since_startup(scratch: Path, root: Path) -> None:
    command = _command(scratch)
    [pid] = _first_instance(scratch, root, command)
    LanguageServerStates.hold_everywhere()

    client = _client(command, scratch)
    client.start()

    assert client.process is not None and client.process.pid == pid
    assert HOLDINGS.take(LanguageServerStates(command, str(scratch))) is None
    assert client.send_request("workspace/symbol", {"query": ""}) == "workspace/symbol"
    client.stop()


def test_the_consumed_prefix_of_a_servers_stdout_is_freed_and_a_next_instance_reads_on_from_its_offset(scratch: Path, root: Path) -> None:
    command = _command(scratch)

    def offset_saved_through_stdout() -> bool:
        # a caller has its answer before the reader saves the offset past it, so the instance is killed only once that is saved
        [path] = _state_files(root)
        state = json.loads(path.read_text())
        return state["stdout_offset"] == os.stat(supervise.Record.load(Path(state["record"])).stdout).st_size

    [pid] = _first_instance(scratch, root, command, big=5, settled=offset_saved_through_stdout)
    [path] = _state_files(root)
    state = json.loads(path.read_text())
    stdout = Path(supervise.Record.load(Path(state["record"])).stdout)

    status = os.stat(stdout)
    assert state["stdout_offset"] == status.st_size
    assert status.st_blocks * 512 < status.st_size - (1 << 20)

    client = _client(command, scratch)
    client.start()
    assert client.process is not None and client.process.pid == pid
    assert client.send_request("workspace/symbol", {"query": ""}) == "workspace/symbol"
    client.stop()
