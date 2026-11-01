"""
The owner of the records a serena keeps (decision://brain/daemon_restart_survival/journal/instance): the daemon on the
default port keeps them under ``serena``, a serena listening on any other port under ``serena-<port>``, so that it starts
beside a live daemon and keeps its calls apart from the daemon's, and a serena listening on no port, as one serving stdio
does, opens no call journal and takes up nothing an ended instance left, so that it too starts beside a live daemon and
leaves the daemon's records alone. The processes such a serena starts are recorded under ``serena`` naming it, and the
daemon's startup adoption leaves them to it while it runs. Each serena here is a real ``serena start-mcp-server``.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any

import psutil
import pytest

from serena.call_journal import DEFAULT_PORT, SERVER, CallJournal, daemon_owner
from solidlsp.ls_survival import LANGUAGE_SERVER_KIND
from solidlsp.structural.bridge_process import BRIDGE_KIND
from solidlsp.util import supervise
from test.serena.test_call_journal import _Holder
from test.serena.test_restart_survival import _FIXTURE, _Serena
from test.serena.test_startup_adoption import _first_instance_killed_while, _records

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")

_DOCUMENT = "test_repo/models.py"


class _StdioSerena:
    """A serena serving MCP over its standard streams, as a client that starts it as a subprocess speaks to it."""

    def __init__(self, root: Path, log: IO[bytes]) -> None:
        """
        :param root: the directory of the records serena keeps.
        :param log: the file serena's stderr is written to.
        """
        command = [
            sys.executable,
            "-c",
            "from serena.cli import top_level; top_level()",
            "start-mcp-server",
            "--transport",
            "stdio",
            "--enable-web-dashboard",
            "false",
            "--open-web-dashboard",
            "false",
        ]
        self.process = subprocess.Popen(
            command,
            env={**os.environ, supervise.ROOT_ENV: str(root)},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=log,
            start_new_session=True,
        )
        self._next_id = 0

    def _send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """:return: the JSON-RPC message answering the request, which serena writes before it reads another."""
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params})
        assert self.process.stdout is not None
        while line := self.process.stdout.readline():
            message = json.loads(line)
            if message.get("id") == self._next_id:
                return message
        raise AssertionError(f"serena ended with exit code {self.process.wait()} before it answered {method}")

    def initialize(self) -> None:
        params = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}
        assert "result" in self.request("initialize", params)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self.request("tools/call", {"name": tool, "arguments": arguments})["result"]
        assert not result.get("isError"), result
        return result

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()
        for stream in (self.process.stdin, self.process.stdout):
            if stream is not None:
                stream.close()


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The serenas' root, whose processes, of whichever owner, are stopped after the test whatever it left running."""
    directory = tmp_path / "restart"
    directory.mkdir()
    monkeypatch.setenv(supervise.ROOT_ENV, str(directory))
    yield directory
    for path in directory.glob("*/process/*.json"):
        if "ended_at" not in json.loads(path.read_text()):
            subprocess.run([supervise.program(), "stop", "-signal", "SIGKILL", str(path)], check=False, stderr=subprocess.DEVNULL)


@pytest.fixture
def daemon(root: Path) -> Iterator[_Holder]:
    """A live instance holding the default daemon's journal, as the daemon on the default port holds it."""
    holder = _Holder("hold")
    yield holder
    holder.kill()


@pytest.fixture
def stdio(root: Path, tmp_path: Path) -> Iterator[Any]:
    started: list[_StdioSerena] = []
    log = (tmp_path / "serena.log").open("ab")

    def start() -> _StdioSerena:
        serena = _StdioSerena(root, log)
        started.append(serena)
        return serena

    yield start
    for serena in started:
        serena.close()
    log.close()


@pytest.mark.parametrize(("port", "owner"), [(DEFAULT_PORT, SERVER), (None, SERVER), (8000, f"{SERVER}-8000")])
def test_the_owner_is_serena_on_the_default_port_or_none_and_names_any_other_port(port: int | None, owner: str) -> None:
    assert daemon_owner(port) == owner


def test_a_serena_on_another_port_starts_beside_a_live_daemon_and_journals_its_calls_under_its_own_owner(
    daemon: _Holder, root: Path, tmp_path: Path
) -> None:
    port = _free_port()
    serena = _Serena(port, root, tmp_path / "serena.log")
    try:
        answer = serena.call("get_current_config", {}, call_id="own")
    finally:
        serena.close()

    assert not answer.get("isError"), answer
    assert (root / f"{SERVER}-{port}" / "call" / "own.json").exists()
    assert not (root / SERVER / "call" / "own.json").exists()


def test_a_stdio_serena_starts_beside_a_live_daemon(daemon: _Holder, stdio: Any) -> None:
    serena = stdio()

    serena.initialize()

    assert serena.process.poll() is None


def test_a_stdio_serena_leaves_the_records_of_an_ended_instance_alone(root: Path, stdio: Any) -> None:
    _first_instance_killed_while(root, "left", "inline", "/bin/sleep 300")
    entry = root / SERVER / "call" / "left.json"
    record_before = [(record.path, record.call_id) for record in _records(root)]
    entry_before = entry.read_bytes()

    stdio().initialize()

    assert entry.read_bytes() == entry_before
    assert [(record.path, record.call_id) for record in _records(root)] == record_before


def test_the_language_server_of_a_stdio_serena_is_recorded_under_serena_and_left_to_it_by_the_daemons_startup_adoption(
    root: Path, stdio: Any, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    shutil.copytree(_FIXTURE, project)
    serena = stdio()
    serena.initialize()
    serena.call("activate_project", {"project": str(project)})
    serena.call("cursor_overview", {"relative_path": _DOCUMENT, "because": "to start the language server"})
    servers = [record for record in _records(root) if record.waiting.kind == LANGUAGE_SERVER_KIND]
    assert servers, "serena started no language server"

    # the daemon's startup adoption, under the journal the stdio serena never opened, with no state naming the server
    journal = CallJournal()
    try:
        supervise.Adoption(journal, (LANGUAGE_SERVER_KIND, BRIDGE_KIND), kept_records=()).adopt()
    finally:
        journal.close()

    assert [record.waiting.instance_pid for record in servers] == [serena.process.pid] * len(servers)
    assert [record.path for record in _records(root) if record.waiting.kind == LANGUAGE_SERVER_KIND] == [record.path for record in servers]
    assert all(_running(record) for record in servers)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _running(record: supervise.Record) -> bool:
    pid = json.loads(record.path.read_text())["pid"]
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
