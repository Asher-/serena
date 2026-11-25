"""
A large symbols cache never stops serena: while one session's call reads and writes a project's cache of about 75k
files, a real ``serena start-mcp-server`` answers every request of another session, its tools/list and its tool calls
alike, within the deadline a client gives a request.
"""

import json
import os
import pickle
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from serena.call_journal import daemon_owner
from serena.config.serena_config import SerenaPaths
from serena.constants import SERENA_MANAGED_DIR_NAME
from serena.util.file_system import IgnoreSpecs
from solidlsp.ls import SolidLanguageServer
from solidlsp.ls_config import LanguageServerConfig, LanguageServerId
from solidlsp.settings import SolidLSPSettings
from solidlsp.util import supervise

pytestmark = [
    pytest.mark.python,
    pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program"),
]

# the files of the cache whose load stopped the daemon for 7.5 minutes (bug://serena/daemon/cache/symbols/load/freeze)
_CACHED_FILES = 75_957
# the most a client waits for an answer to tools/list or to a quick tool call
_DEADLINE = 2.0
# the most a call that reads or writes the large cache may take
_CALL_SECONDS = 600.0
_MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


class _Session:
    """One MCP session with the daemon, over a connection pool of its own."""

    def __init__(self, url: str, timeout: float) -> None:
        self._url = url
        self._client = httpx.Client(timeout=timeout, headers=_MCP_HEADERS)
        self._session: str | None = None
        self._next_id = 0

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        :return: the result of the JSON-RPC request
        """
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        headers = {"Mcp-Session-Id": self._session} if self._session else {}
        response = self._client.post(self._url, json=message, headers=headers)
        response.raise_for_status()
        self._session = response.headers.get("mcp-session-id", self._session)
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()["result"]
        for line in response.text.splitlines():
            if line.startswith("data:"):
                decoded = json.loads(line[len("data:") :])
                if decoded.get("id") == self._next_id:
                    return decoded["result"]
        raise AssertionError(f"no answer to {method}: {response.text[:300]}")

    def initialize(self) -> None:
        params = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}
        self.request("initialize", params)
        self._client.post(
            self._url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers={"Mcp-Session-Id": self._session or ""}
        )

    def call(self, tool: str, arguments: dict[str, Any]) -> str:
        """
        :return: the text of the tool's answer, which must not be an error
        """
        result = self.request("tools/call", {"name": tool, "arguments": arguments})
        assert not result.get("isError"), result
        return result["content"][0]["text"]

    def close(self) -> None:
        self._client.close()


class _Daemon:
    """
    A ``serena start-mcp-server`` over streamable HTTP, with a home directory of its own and a directory of its own for
    the records of the processes it starts.
    """

    def __init__(self, home: Path, records: Path, log: Path) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/mcp"
        self._process_records = records / daemon_owner(port) / "process"
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
                command,
                env={**os.environ, "SERENA_HOME": str(home), supervise.ROOT_ENV: str(records)},
                stdout=output,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )

    def session(self, timeout: float) -> _Session:
        """
        :return: a new, initialized session, once the daemon listens
        """
        session = _Session(self.url, timeout)
        deadline = time.monotonic() + _CALL_SECONDS
        while True:
            try:
                session.initialize()
                return session
            except httpx.TransportError:
                assert self.process.poll() is None, "serena ended while starting"
                assert time.monotonic() < deadline, "serena did not start listening"
                time.sleep(0.05)

    def close(self) -> None:
        """
        Ends the daemon, then every process it started that its end did not end.
        """
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
        for record in self._process_records.glob("*.json"):
            if "ended_at" not in json.loads(record.read_text()):
                subprocess.run([supervise.program(), "stop", "-signal", "SIGKILL", str(record)], check=False, stderr=subprocess.DEVNULL)


def _project(root: Path, file_count: int) -> Path:
    """
    :return: a python project of the given number of modules, each defining one function, with a data folder inside it
    """
    (root / SERENA_MANAGED_DIR_NAME).mkdir(parents=True)
    (root / SERENA_MANAGED_DIR_NAME / "project.yml").write_text("project_name: project\nlanguages:\n  - python\n", encoding="utf-8")
    for index in range(file_count):
        (root / f"module_{index}.py").write_text(f"def function_{index}():\n    return {index}\n", encoding="utf-8")
    return root


def _cached_symbols(index: int) -> list[dict[str, Any]]:
    """
    :return: a file's symbols as a language server reports them: a class of five methods
    """

    def symbol(name: str, kind: int, line: int, children: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        found: dict[str, Any] = {
            "name": name,
            "kind": kind,
            "range": {"start": {"line": line, "character": 0}, "end": {"line": line + 3, "character": 0}},
            "selectionRange": {"start": {"line": line, "character": 4}, "end": {"line": line, "character": 4 + len(name)}},
        }
        if children is not None:
            found["children"] = children
        return found

    return [symbol(f"Class{index}", 5, 0, [symbol(f"method_{index}_{m}", 6, 1 + 4 * m) for m in range(5)])]


def _cached_key(index: int) -> str:
    return f"vendor/package_{index // 100}/module_{index}.py"


def _cache_dir(root: Path) -> Path:
    return root / SERENA_MANAGED_DIR_NAME / SolidLanguageServer.CACHE_FOLDER_NAME / LanguageServerId.PYTHON.get_key()


@dataclass(frozen=True)
class _Answer:
    """A request of the second session and how long its answer took."""

    sent: float
    """the time the request was sent, in seconds since the epoch"""
    seconds: float
    """the seconds until it was answered, or until the client gave up on it"""

    def overlaps(self, start: float, end: float) -> bool:
        """
        :return: whether the request was waiting for its answer at some time between start and end
        """
        return self.sent <= end and self.sent + self.seconds >= start


def _answered_while(work: threading.Thread, session: _Session) -> list[_Answer]:
    """
    Sends the session's tools/list and get_current_config, one after the other, for as long as the work runs, or until
    the session gives up on one, after which it sends no more.

    :return: the requests sent and how long each answer took
    """
    answers: list[_Answer] = []
    requests = (lambda: session.request("tools/list", {}), lambda: session.call("get_current_config", {}))
    while work.is_alive():
        for request in requests:
            sent, started = time.time(), time.monotonic()
            try:
                request()
            except httpx.TimeoutException:
                answers.append(_Answer(sent, time.monotonic() - started))
                return answers
            answers.append(_Answer(sent, time.monotonic() - started))
    return answers


def _logged_at(log: Path, marker: str) -> float:
    """
    :return: the time of the first line of the daemon's log holding the marker, in seconds since the epoch
    """
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if marker in line:
            return datetime.strptime(" ".join(line.split()[1:3]), "%Y-%m-%d %H:%M:%S,%f").timestamp()
    raise AssertionError(f"the daemon logged no line holding {marker!r}")


@pytest.fixture
def daemon(tmp_path: Path) -> Iterator[_Daemon]:
    home = tmp_path / "home"
    home.mkdir()
    started = _Daemon(home, tmp_path / "records", tmp_path / "serena.log")
    yield started
    started.close()


def _run_meanwhile(calls: list[tuple[str, dict[str, Any]]], session: _Session) -> tuple[threading.Thread, list[BaseException]]:
    """
    :return: a started thread making the session's calls in order, and the exceptions they raised
    """
    failures: list[BaseException] = []

    def run() -> None:
        try:
            for tool, arguments in calls:
                session.call(tool, arguments)
        except BaseException as exception:
            failures.append(exception)

    thread = threading.Thread(target=run, name="first-session-calls")
    thread.start()
    return thread, failures


def test_a_large_cache_of_the_former_format_stops_no_other_session(daemon: _Daemon, tmp_path: Path) -> None:
    # caches of about 75k files in the former whole-file format, of both levels
    root = _project(tmp_path / "project", 3)
    cache_dir = _cache_dir(root)
    cache_dir.mkdir(parents=True)
    cached = {_cached_key(index): (f"hash-{index}", _cached_symbols(index)) for index in range(_CACHED_FILES)}
    for name in ("raw_document_symbols.pkl", "document_symbols.pkl"):
        with (cache_dir / name).open("wb") as output:
            pickle.dump({"__cache_version": "former", "obj": cached}, output)
    del cached

    first = daemon.session(_CALL_SECONDS)
    second = daemon.session(_DEADLINE)
    try:
        work, failures = _run_meanwhile(
            [
                ("activate_project", {"project": str(root)}),
                ("cursor_overview", {"relative_path": "module_0.py", "because": "to have the project's cache read and written"}),
            ],
            first,
        )
        answers = _answered_while(work, second)
        work.join()
    finally:
        first.close()
        second.close()

    # the cache's own window: from the making of the language server, which opens its cache dir, removes the former
    # caches and opens the store, to the start of its process. The deadline is asserted over this window only: the
    # process start after it stops every thread on its own (symptom://serena/daemon/language_server/start/stall)
    log = tmp_path / "serena.log"
    window = (_logged_at(log, "Creating language server instance"), _logged_at(log, "Starting language server python for"))
    in_window = [answer for answer in answers if answer.overlaps(*window)]
    assert failures == []
    assert in_window, f"no request of the second session was waiting during the cache's window {window}"
    assert max(answer.seconds for answer in in_window) < _DEADLINE


def test_a_large_store_read_and_written_by_a_whole_project_search_stops_no_other_session(daemon: _Daemon, tmp_path: Path) -> None:
    # imported here, so that the test of the former format runs where no store exists
    from solidlsp.util.symbol_store import SymbolStore

    # a store holding the symbols of about 75k files beside the project's own 300, which none of it holds yet
    root = _project(tmp_path / "project", 300)
    config = LanguageServerConfig(
        ls_id=LanguageServerId.PYTHON, ignore_spec_of=IgnoreSpecs(str(root), [], read_gitignore_files=True).spec_of
    )
    settings = SolidLSPSettings(solidlsp_dir=SerenaPaths().serena_user_home_dir, project_data_path=str(root / SERENA_MANAGED_DIR_NAME))
    server = SolidLanguageServer.create(config, str(root), solidlsp_settings=settings)
    store = SymbolStore(
        server.cache_dir / SolidLanguageServer.RAW_DOCUMENT_SYMBOL_STORE_FILENAME,
        version=server._raw_document_symbols_cache_version(),
        loader=SolidLanguageServer._cache_loader,
    )
    for index in range(_CACHED_FILES):
        store.put(_cached_key(index), f"hash-{index}", _cached_symbols(index))
    store.close()

    first = daemon.session(_CALL_SECONDS)
    second = daemon.session(_DEADLINE)
    search = {"name_path_pattern": "function_", "substring_matching": True, "because": "to have every file's symbols read and written"}
    try:
        work, failures = _run_meanwhile(
            [("activate_project", {"project": str(root)}), ("cursor_find", search), ("cursor_find", search)],
            first,
        )
        answers = _answered_while(work, second)
        work.join()
    finally:
        first.close()
        second.close()

    seconds = [answer.seconds for answer in answers]
    assert failures == []
    assert seconds and max(seconds) < _DEADLINE
