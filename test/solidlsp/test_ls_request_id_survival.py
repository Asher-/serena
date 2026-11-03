"""
A request in flight when serena is killed is not answered to the next instance's request
(task://daemon_restart_survival.15): the adopting instance numbers its requests past every id the ended
instance handed out, so the late answer to the dead request matches no pending request.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from solidlsp.ls_config import LanguageServerId
from solidlsp.ls_process import StdioLanguageServer, ProcessLaunchInfo
from solidlsp.util import supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")

SERVER = textwrap.dedent(
    """
    import json, sys
    journal = open(sys.argv[1], "a", buffering=1)
    held = None
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
        if method == "workspace/held":
            held = message["id"]
        elif "id" in message:
            if held is not None:
                answer(held, "held")
                held = None
            answer(message["id"], method)
    """
)

FIRST_INSTANCE = textwrap.dedent(
    """
    import json, logging, sys, threading, time
    from solidlsp.ls_config import LanguageServerId
    from solidlsp.ls_process import StdioLanguageServer, ProcessLaunchInfo
    process = StdioLanguageServer(ProcessLaunchInfo(cmd=json.loads(sys.argv[1]), cwd=sys.argv[2]), LanguageServerId.PYTHON, lambda _: logging.INFO)
    process.start()
    process.send_request("initialize", {"processId": None})
    threading.Thread(target=process.send_request, args=("workspace/held", {}), daemon=True).start()
    time.sleep(300)
    """
)


@pytest.fixture(autouse=True)
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(supervise.ROOT_ENV, str(tmp_path / "restart"))
    return tmp_path


def test_the_next_instance_never_receives_the_answer_to_a_request_in_flight_at_the_kill(tmp_path: Path) -> None:
    server = tmp_path / "server.py"
    server.write_text(SERVER)
    journal = tmp_path / "journal"
    command = [sys.executable, str(server), str(journal)]

    # the first instance holds a request open at the server, and is killed with it in flight
    instance = subprocess.Popen(
        [sys.executable, "-c", FIRST_INSTANCE, json.dumps(command), str(tmp_path)],
        start_new_session=True,
    )
    deadline = time.monotonic() + 60
    while "workspace/held" not in (journal.read_text() if journal.exists() else ""):
        assert time.monotonic() < deadline, "the first instance never sent its held request"
        time.sleep(0.05)
    os.killpg(instance.pid, signal.SIGKILL)
    instance.wait()

    # the next instance's request is answered with its own answer, not the held one
    process = StdioLanguageServer(ProcessLaunchInfo(cmd=command, cwd=str(tmp_path)), LanguageServerId.PYTHON, lambda _: logging.INFO)
    process.start()
    assert process.send_request("workspace/symbol", {"query": ""}) == "workspace/symbol"
    process.stop()
