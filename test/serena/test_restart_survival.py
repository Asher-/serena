"""
Serena's restart, live: a real ``serena start-mcp-server`` over a fixture project with a real language server, spoken to
over streamable HTTP as the multiplexer speaks to it. Everything serena started must run on through a restart of serena,
and the next instance must take it over and answer for the calls the ended one held.
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import psutil
import pytest

from serena.call_journal import CALL_ID_HEADER, RESUME_METHOD
from solidlsp.util import supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")

_FIXTURE = Path(__file__).parents[1] / "resources" / "repos" / "python" / "test_repo"
_DOCUMENT = "test_repo/models.py"
_START_SECONDS = 60.0
_LANGUAGE_SERVER_KIND = "solidlsp.language_server"
_MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@dataclass(frozen=True)
class _Started:
    """A process as its record names it."""

    record: Path
    pid: int
    start: str


class _Serena:
    """One instance of serena, in a process group of its own as launchd gives it one."""

    def __init__(self, port: int, root: Path, log: Path) -> None:
        self.url = f"http://127.0.0.1:{port}/mcp"
        self._client = httpx.Client(timeout=_START_SECONDS, headers=_MCP_HEADERS)
        self._session: str | None = None
        self._next_id = 0
        environment = {**os.environ, supervise.ROOT_ENV: str(root)}
        command = [
            sys.executable,
            "-c",
            "from serena.cli import top_level; top_level()",
            "start-mcp-server",
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--enable-web-dashboard",
            "false",
            "--open-web-dashboard",
            "false",
        ]
        with log.open("ab") as output:
            self.process = subprocess.Popen(
                command, env=environment, stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True
            )
        self._initialize()

    def _request(self, method: str, params: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        sent = dict(headers or {})
        if self._session:
            sent["Mcp-Session-Id"] = self._session
        response = self._client.post(self.url, json=message, headers=sent)
        response.raise_for_status()
        self._session = response.headers.get("mcp-session-id", self._session)
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        for line in response.text.splitlines():
            if line.startswith("data:"):
                decoded = json.loads(line[len("data:") :])
                if decoded.get("id") == self._next_id:
                    return decoded
        raise AssertionError(f"no answer to {method}: {response.text[:300]}")

    def _initialize(self) -> None:
        params = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}
        deadline = time.monotonic() + _START_SECONDS
        while True:
            try:
                self._request("initialize", params)
                break
            except httpx.TransportError:
                assert self.process.poll() is None, "serena ended while starting"
                assert time.monotonic() < deadline, "serena did not start listening"
                time.sleep(0.05)
        sent = {"Mcp-Session-Id": self._session or ""}
        self._client.post(self.url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=sent)

    def call(self, tool: str, arguments: dict[str, Any], call_id: str = "") -> dict[str, Any]:
        headers = {CALL_ID_HEADER: call_id} if call_id else {}
        return self._request("tools/call", {"name": tool, "arguments": arguments}, headers)["result"]

    def overview(self, document: str = _DOCUMENT, call_id: str = "") -> str:
        arguments = {"relative_path": document, "because": "to read what the language server holds of the document"}
        result = self.call("cursor_overview", arguments, call_id)
        assert not result.get("isError"), result
        return _symbols(result["content"][0]["text"])

    def activate(self, project: Path) -> None:
        result = self.call("activate_project", {"project": str(project)})
        assert not result.get("isError"), result

    def resume(self, call_id: str) -> dict[str, Any]:
        return self._request(RESUME_METHOD, {"call_id": call_id})["result"]

    def kill(self) -> None:
        os.killpg(self.process.pid, signal.SIGKILL)
        self.process.wait()

    def terminate(self) -> int:
        self.process.send_signal(signal.SIGTERM)
        return self.process.wait(timeout=30)

    def close(self) -> None:
        self._client.close()
        if self.process.poll() is None:
            self.kill()


def _symbols(overview: str) -> str:
    """:return: the listing's symbols, without the version line that names the moment it was read."""
    return "\n".join(line for line in overview.splitlines() if ":" in line and "@" in line)


def _language_servers(root: Path) -> list[_Started]:
    started = []
    for path in (root / supervise.OWNER / "process").glob("*.json"):
        fields = json.loads(path.read_text())
        if (fields.get("waiting") or {}).get("kind") == _LANGUAGE_SERVER_KIND and "ended_at" not in fields:
            started.append(_Started(path, fields["pid"], fields["start"]))
    return started


def _running(started: _Started) -> bool:
    try:
        process = psutil.Process(started.pid)
    except psutil.NoSuchProcess:
        return False
    return process.status() != psutil.STATUS_ZOMBIE


def _initialize_answers(started: _Started) -> int:
    """:return: how many answers to initialize the server has written, each the only message holding serverInfo or capabilities."""
    stdout = Path(json.loads(started.record.read_text())["stdout"]).read_text(errors="replace")
    return stdout.count('"capabilities"')


@pytest.fixture
def root(tmp_path: Path) -> Iterator[Path]:
    """The instances' root, whose processes are stopped after the test whatever it left running."""
    directory = tmp_path / "restart"
    directory.mkdir()
    yield directory
    for path in (directory / supervise.OWNER / "process").glob("*.json"):
        if "ended_at" not in json.loads(path.read_text()):
            subprocess.run([supervise.program(), "stop", "-signal", "SIGKILL", str(path)], check=False, stderr=subprocess.DEVNULL)


@pytest.fixture
def port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    copied = tmp_path / "project"
    shutil.copytree(_FIXTURE, copied)
    return copied


@pytest.fixture
def instances(port: int, root: Path, tmp_path: Path) -> Iterator[list[_Serena]]:
    started: list[_Serena] = []

    class Start(list):  # type: ignore[type-arg]
        def __call__(self) -> _Serena:
            instance = _Serena(port, root, tmp_path / "serena.log")
            started.append(instance)
            return instance

    yield Start()  # type: ignore[misc]
    for instance in started:
        instance.close()


def _adopted_after(end_first: str, instances: Any, project: Path, root: Path) -> None:
    first = instances()
    first.activate(project)
    before = first.overview()
    servers = _language_servers(root)
    assert servers, "serena started no language server"

    if end_first == "kill":
        first.kill()
    else:
        first.terminate()
    assert all(_running(server) for server in servers), "a language server ended with serena"

    second = instances()
    second.activate(project)

    assert second.overview() == before
    assert _language_servers(root) == servers
    assert all(_running(server) for server in servers)
    assert [_initialize_answers(server) for server in servers] == [1] * len(servers)


def test_a_language_server_outlives_a_kill_of_serenas_group_and_the_next_instance_adopts_it(
    instances: Any, project: Path, root: Path
) -> None:
    _adopted_after("kill", instances, project, root)


def test_a_language_server_outlives_a_terminate_of_serena_and_the_next_instance_adopts_it(
    instances: Any, project: Path, root: Path
) -> None:
    _adopted_after("terminate", instances, project, root)


def test_resume_answers_a_call_finished_before_the_kill_with_its_result(instances: Any, project: Path) -> None:
    first = instances()
    first.activate(project)
    answer = first.overview(call_id="finished")
    first.kill()

    second = instances()
    resumed = second.resume("finished")

    assert resumed["isError"] is False
    assert _symbols(resumed["content"][0]["text"]) == answer


def test_resume_answers_a_call_in_flight_at_the_kill_with_the_restart(instances: Any, project: Path, root: Path) -> None:
    first = instances()
    first.activate(project)
    journaled = root / supervise.OWNER / "call" / "in-flight.json"

    def call() -> None:
        try:
            first.overview(call_id="in-flight")
        except (httpx.TransportError, AssertionError):
            pass

    threading.Thread(target=call, daemon=True).start()
    deadline = time.monotonic() + _START_SECONDS
    while not journaled.exists():
        assert time.monotonic() < deadline, "the call was not journaled"
        time.sleep(0.001)
    first.kill()

    second = instances()
    resumed = second.resume("in-flight")

    assert resumed["isError"] is True
    assert "restarted" in resumed["content"][0]["text"]
    assert second.resume("in-flight") == resumed


def test_resume_of_a_call_serena_never_received_is_a_failed_result(instances: Any) -> None:
    resumed = instances().resume("never-sent")

    assert resumed["isError"] is True
    assert "never-sent" in resumed["content"][0]["text"]
