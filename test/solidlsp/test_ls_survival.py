"""
A language server outlives the serena that started it and is adopted by the next
(task://daemon_restart_survival.15): the same process answers, with no second initialize and no second
didOpen, for a document opened before the kill. The server here is a small stand-in speaking LSP, so
what it received can be read from its journal.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from solidlsp.ls_config import Language
from solidlsp.ls_process import LanguageServerProcess, ProcessLaunchInfo
from solidlsp.ls_survival import LanguageServerStateFile
from solidlsp.util import supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")

SERVER = textwrap.dedent(
    """
    import json, sys
    journal = open(sys.argv[1], "a", buffering=1)
    opened = set()
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
        journal.write(method + "\\n")
        if method == "initialize":
            answer(message["id"], {"capabilities": {"documentSymbolProvider": True}})
        elif method == "textDocument/didOpen":
            opened.add(message["params"]["textDocument"]["uri"])
        elif method == "textDocument/documentSymbol":
            answer(message["id"], [{"name": "open" if message["params"]["textDocument"]["uri"] in opened else "closed"}])
        elif "id" in message:
            answer(message["id"], None)
    """
)

FIRST_INSTANCE = textwrap.dedent(
    """
    import json, logging, sys, time
    from solidlsp.ls_config import Language
    from solidlsp.ls_process import LanguageServerProcess, ProcessLaunchInfo
    process = LanguageServerProcess(ProcessLaunchInfo(cmd=json.loads(sys.argv[1]), cwd=sys.argv[2]), Language.PYTHON, lambda _: logging.INFO)
    process.start()
    process.send_request("initialize", {"processId": None})
    process.send_notification("initialized", {})
    process.send_notification("textDocument/didOpen", {"textDocument": {"uri": "file:///a", "version": 1, "languageId": "python", "text": "a"}})
    print(process.process.pid, process.send_request("textDocument/documentSymbol", {"textDocument": {"uri": "file:///a"}})[0]["name"], flush=True)
    time.sleep(300)
    """
)


@pytest.fixture(autouse=True)
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(supervise.ROOT_ENV, str(tmp_path / "restart"))
    return tmp_path


def _start_and_kill_the_first_instance(tmp_path: Path, command: list[str]) -> tuple[int, str]:
    instance = subprocess.Popen(
        [sys.executable, "-c", FIRST_INSTANCE, json.dumps(command), str(tmp_path)],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert instance.stdout is not None
    pid, symbol = instance.stdout.readline().split()
    os.killpg(instance.pid, signal.SIGKILL)
    instance.wait()
    return int(pid), symbol


def test_the_next_instance_adopts_the_server_with_no_second_initialize_or_did_open(tmp_path: Path) -> None:
    server = tmp_path / "server.py"
    server.write_text(SERVER)
    journal = tmp_path / "journal"
    command = [sys.executable, str(server), str(journal)]
    pid, symbol = _start_and_kill_the_first_instance(tmp_path, command)
    assert symbol == "open"

    # the next instance, knowing nothing but the command and the directory
    process = LanguageServerProcess(ProcessLaunchInfo(cmd=command, cwd=str(tmp_path)), Language.PYTHON, lambda _: logging.INFO)
    process.start()
    assert process.process is not None and process.process.pid == pid
    assert process.send_request("initialize", {"processId": None}) == {"capabilities": {"documentSymbolProvider": True}}
    process.send_notification("initialized", {})
    process.send_notification(
        "textDocument/didOpen", {"textDocument": {"uri": "file:///a", "version": 1, "languageId": "python", "text": "a"}}
    )
    assert process.send_request("textDocument/documentSymbol", {"textDocument": {"uri": "file:///a"}}) == [{"name": "open"}]

    methods = journal.read_text().split()
    assert methods.count("initialize") == 1
    assert methods.count("initialized") == 1
    assert methods.count("textDocument/didOpen") == 1
    process.stop()
    assert not LanguageServerStateFile(command, str(tmp_path)).path.exists()


def test_a_document_changed_while_no_instance_ran_is_brought_up_to_date(tmp_path: Path) -> None:
    server = tmp_path / "server.py"
    server.write_text(SERVER)
    journal = tmp_path / "journal"
    command = [sys.executable, str(server), str(journal)]
    _start_and_kill_the_first_instance(tmp_path, command)

    process = LanguageServerProcess(ProcessLaunchInfo(cmd=command, cwd=str(tmp_path)), Language.PYTHON, lambda _: logging.INFO)
    process.start()
    process.send_notification(
        "textDocument/didOpen", {"textDocument": {"uri": "file:///a", "version": 1, "languageId": "python", "text": "b"}}
    )
    process.send_request("textDocument/documentSymbol", {"textDocument": {"uri": "file:///a"}})
    methods = journal.read_text().split()
    assert methods.count("textDocument/didOpen") == 1
    assert methods.count("textDocument/didChange") == 1
    process.stop()
