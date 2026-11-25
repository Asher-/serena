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
from pathlib import Path
from typing import Any

import httpx
import pytest

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
    """A ``serena start-mcp-server`` over streamable HTTP, with a home directory of its own."""

    def __init__(self, home: Path, log: Path) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/mcp"
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
                env={**os.environ, "SERENA_HOME": str(home)},
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
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()


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


def _answered_while(work: threading.Thread, session: _Session) -> list[float]:
    """
    Sends the session's tools/list and get_current_config, one after the other, for as long as the work runs.

    :return: the seconds each of them took to be answered
    """
    seconds: list[float] = []
    while work.is_alive():
        started = time.monotonic()
        tools = session.request("tools/list", {})
        seconds.append(time.monotonic() - started)
        assert tools["tools"]
        started = time.monotonic()
        session.call("get_current_config", {})
        seconds.append(time.monotonic() - started)
    return seconds


@pytest.fixture
def daemon(tmp_path: Path) -> Iterator[_Daemon]:
    home = tmp_path / "home"
    home.mkdir()
    started = _Daemon(home, tmp_path / "serena.log")
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
        seconds = _answered_while(work, second)
        work.join()
    finally:
        first.close()
        second.close()

    assert failures == []
    assert seconds and max(seconds) < _DEADLINE


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
        seconds = _answered_while(work, second)
        work.join()
    finally:
        first.close()
        second.close()

    assert failures == []
    assert seconds and max(seconds) < _DEADLINE
