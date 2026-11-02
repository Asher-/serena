"""
The call journal: an instance holds the journal's lock or is refused it; a call id is recorded once and its entry never
overwritten; ``brain/resume`` answers a call with its answer, waits for a call an instance is still answering, answers a
call whose instance ended with the restart, and answers a call the journal never received with error code -32003; and a
``tools/call`` FastMCP serves is journaled where its work ends and never run twice under one call id.
"""

import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from starlette.testclient import TestClient

from serena import call_journal
from serena.call_journal import CALL_ID_HEADER, RESUME_METHOD, CallJournal, CallJournalLayer, CallResult, NoCallError
from serena.mcp import _JournaledFastMCP
from solidlsp.util import supervise


@pytest.fixture(autouse=True)
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(supervise.ROOT_ENV, str(tmp_path))
    return tmp_path


class _Waits(threading.Condition):
    """A journal's condition that records the first time a ``resume`` waits on it."""

    def __init__(self, lock: Any) -> None:
        super().__init__(lock)
        self.waiting = threading.Event()

    def wait(self, timeout: float | None = None) -> bool:
        self.waiting.set()
        return super().wait(timeout)


def _watched(journal: CallJournal) -> _Waits:
    """:return: the condition the journal's resumes now wait on, which records that one waits."""
    waits = _Waits(journal._lock)
    journal._finished = waits
    return waits


class _Resume(threading.Thread):
    """A ``resume`` of a call, run on a thread of its own."""

    def __init__(self, journal: CallJournal, call_id: str) -> None:
        super().__init__(daemon=True)
        self._journal = journal
        self._call_id = call_id
        self.answers: list[CallResult] = []

    def run(self) -> None:
        self.answers.append(self._journal.resume(self._call_id))


def _await_wait(waits: _Waits, resume: threading.Thread) -> None:
    """Returns once the resume waits on the journal's condition, or has returned without waiting."""
    while not waits.waiting.is_set() and resume.is_alive():
        waits.waiting.wait(0.01)


_HOLDER = textwrap.dedent(
    """
    import sys
    from serena.call_journal import CallJournal, CallResult
    journal = CallJournal()
    if sys.argv[1] == "release":
        journal.receive("held", "the_tool", {})
        journal.close()
    print("ready", flush=True)
    sys.stdin.readline()
    if sys.argv[1] == "release":
        journal.finish("held", CallResult("done"))
    """
)


class _Holder:
    """
    Another live instance: a process that opens the journal and then either holds its lock (``hold``) or receives the
    call ``held`` and releases the lock (``release``), finishing that call once it reads a line.
    """

    def __init__(self, mode: str) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, mode], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=dict(os.environ)
        )
        assert self.process.stdout is not None
        assert self.process.stdout.readline() == "ready\n"

    def go_on(self) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write("go\n")
        self.process.stdin.flush()
        assert self.process.wait() == 0

    def kill(self) -> None:
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGKILL)
            self.process.wait()


_ENDED = textwrap.dedent(
    """
    import json, sys
    from serena.call_journal import CallJournal, CallResult
    journal = CallJournal()
    for call_id, answer in json.loads(sys.argv[1]).items():
        journal.receive(call_id, "the_tool", {})
        if answer is not None:
            journal.finish(call_id, CallResult(answer))
    """
)


def _ended_instance(calls: dict[str, str | None]) -> None:
    """
    Runs an instance to its end: a process that receives each call and answers those given an answer.

    :param calls: each call id, with the text of its answer or None for a call left unanswered.
    """
    subprocess.run([sys.executable, "-c", _ENDED, json.dumps(calls)], check=True, env=dict(os.environ))


def test_a_finished_call_is_answered_with_its_result_by_the_next_instance() -> None:
    _ended_instance({"call-1": "found"})

    assert CallJournal().resume("call-1") == CallResult("found")


def test_a_call_that_holds_an_answer_keeps_it_when_a_process_of_the_call_finishes_it_again() -> None:
    _ended_instance({"call-1": "the answer its caller read"})
    journal = CallJournal()
    journal.claim("call-1")

    journal.finish("call-1", CallResult("the process's outcome", True))

    assert journal.resume("call-1") == CallResult("the answer its caller read")


def test_a_call_in_flight_in_this_instance_is_answered_when_it_finishes() -> None:
    journal = CallJournal()
    journal.receive("call-1", "find_symbol", {})
    waits = _watched(journal)
    resume = _Resume(journal, "call-1")
    resume.start()
    _await_wait(waits, resume)

    journal.finish("call-1", CallResult("found"))
    resume.join()

    assert resume.answers == [CallResult("found")]


def test_a_call_lost_with_its_instance_is_answered_with_the_restart_and_the_same_answer_again() -> None:
    _ended_instance({"call-1": None})

    next_instance = CallJournal()
    lost = next_instance.resume("call-1")

    assert lost.is_error
    assert "restarted" in lost.text
    assert next_instance.resume("call-1") == lost


@pytest.mark.parametrize("call_id", ["absent", "../absent", ""])
def test_a_call_the_journal_does_not_hold_is_refused(call_id: str) -> None:
    with pytest.raises(NoCallError):
        CallJournal().resume(call_id)


def test_a_received_call_id_is_not_recorded_again_and_its_entry_is_kept() -> None:
    journal = CallJournal()

    assert journal.receive("call-1", "find_symbol", {"name": "x"}) is True
    assert journal.receive("call-1", "other_tool", {}) is False
    journal.finish("call-1", CallResult("found"))
    assert journal.receive("call-1", "other_tool", {}) is False

    assert journal.resume("call-1") == CallResult("found")


def test_a_call_an_instance_claims_is_answered_once_that_instance_finishes_it() -> None:
    _ended_instance({"call-1": None})
    journal = CallJournal()

    journal.claim("call-1")
    waits = _watched(journal)
    resume = _Resume(journal, "call-1")
    resume.start()
    _await_wait(waits, resume)
    journal.finish("call-1", CallResult("found"))
    resume.join()

    assert resume.answers == [CallResult("found")]


def test_an_instance_is_refused_the_journal_while_another_live_instance_holds_it_and_opens_it_once_that_one_is_killed() -> None:
    holder = _Holder("hold")
    try:
        refused: Exception | None = None
        try:
            CallJournal()
        except Exception as e:
            refused = e
        assert refused is not None, "the journal opened while another live instance held it"
        assert isinstance(refused, call_journal.JournalHeldError)
    finally:
        holder.kill()

    CallJournal().close()


def test_resume_of_a_call_another_live_instance_received_waits_for_its_answer() -> None:
    holder = _Holder("release")
    try:
        journal = CallJournal()
        waits = _watched(journal)
        resume = _Resume(journal, "held")
        resume.start()
        _await_wait(waits, resume)
        assert waits.waiting.is_set(), f"resume answered {resume.answers} while the instance that received the call ran"

        holder.go_on()
        resume.join()
    finally:
        holder.kill()

    assert resume.answers == [CallResult("done")]


class _Listener:
    """An ASGI application standing for FastMCP's listener, answering with the body and status it is given."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.seen: list[bytes] = []

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        event = await receive()
        self.seen.append(event["body"])
        await send({"type": "http.response.start", "status": self.status, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": self.body})


def _layer(listener: _Listener) -> TestClient:
    layer: Any = CallJournalLayer(listener, CallJournal(), "/mcp")
    return TestClient(layer)


def _resume(client: TestClient, params: dict[str, Any]) -> dict[str, Any]:
    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 9, "method": RESUME_METHOD, "params": params})
    assert response.status_code == 200
    return response.json()


def test_a_request_other_than_resume_is_passed_on_whole_and_answered_by_the_listener() -> None:
    listener = _Listener(b"the answer")
    request = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "the_tool", "arguments": {"a": 1}}}

    response = _layer(listener).post("/mcp", json=request, headers={CALL_ID_HEADER: "call-1"})

    assert response.text == "the answer"
    assert json.loads(listener.seen[0]) == request


def test_another_route_is_passed_on_untouched() -> None:
    listener = _Listener(b"ok")

    assert _layer(listener).post("/session/end", content=b"anything").text == "ok"
    assert listener.seen == [b"anything"]


def test_resume_answers_a_call_of_the_ended_instance_in_the_form_of_a_tool_result() -> None:
    _ended_instance({"call-1": "the answer"})

    answer = _resume(_layer(_Listener(b"never reached")), {"call_id": "call-1"})

    assert answer == {"jsonrpc": "2.0", "id": 9, "result": {"content": [{"type": "text", "text": "the answer"}], "isError": False}}


def test_resume_answers_a_call_lost_with_its_instance_with_the_restart() -> None:
    _ended_instance({"call-1": None})

    result = _resume(_layer(_Listener(b"never reached")), {"call_id": "call-1"})["result"]

    assert result["isError"] is True
    assert "restarted" in result["content"][0]["text"]


def test_resume_of_a_call_the_journal_never_received_is_answered_with_error_code_minus_32003() -> None:
    answer = _resume(_layer(_Listener(b"never reached")), {"call_id": "absent"})

    assert "result" not in answer
    assert answer["error"]["code"] == -32003


@pytest.mark.parametrize("params", [{}, {"call_id": ""}, {"call_id": 5}])
def test_resume_that_names_no_call_is_refused_as_invalid(params: dict[str, Any]) -> None:
    assert _resume(_layer(_Listener(b"never reached")), params)["error"]["code"] == -32602


_MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


class _Tools:
    """The tools of the test server, with what their runs left behind."""

    def __init__(self) -> None:
        self.release = threading.Event()
        """lets every run of ``held`` answer."""
        self.started = threading.Event()
        """set once ``held`` runs."""
        self.runs = 0
        """how many times ``held`` ran."""

    async def held(self, text: str) -> str:
        """Answers once released."""
        self.runs += 1
        self.started.set()
        await anyio.to_thread.run_sync(self.release.wait, abandon_on_cancel=True)
        return f"answered {text}"

    async def fails(self) -> str:
        """Fails."""
        raise ValueError("it broke")

    async def call_id(self) -> str:
        """Answers the call id the call runs under."""
        return str(call_journal.CALL_ID_VAR.get())


class _Server:
    """A journaling FastMCP server, spoken to over its streamable-http listener in one MCP session."""

    def __init__(self, client: TestClient) -> None:
        self._client = client
        initialize = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}
        response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": initialize}, headers=_MCP_HEADERS)
        assert response.status_code == 200, response.text
        self.session = response.headers["mcp-session-id"]
        client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=self._headers())

    def _headers(self, call_id: str = "") -> dict[str, str]:
        headers = {**_MCP_HEADERS, "Mcp-Session-Id": self.session}
        if call_id:
            headers[CALL_ID_HEADER] = call_id
        return headers

    def call(self, tool: str, arguments: dict[str, Any], call_id: str = "", request_id: int = 7) -> dict[str, Any]:
        """:return: the JSON-RPC message answering the call."""
        request = {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
        response = self._client.post("/mcp", json=request, headers=self._headers(call_id))
        assert response.status_code == 200, response.text
        for line in response.text.splitlines():
            if line.startswith("data:"):
                message = json.loads(line[len("data:") :])
                if message.get("id") == request_id:
                    return message
        raise AssertionError(f"no answer to the call: {response.text}")

    def cancel(self, request_id: int) -> None:
        notification = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": request_id}}
        assert self._client.post("/mcp", json=notification, headers=self._headers()).status_code == 202


class _Call(threading.Thread):
    """A call to the server, made on a thread of its own."""

    def __init__(self, server: _Server, call_id: str, request_id: int, text: str = "x") -> None:
        super().__init__(daemon=True)
        self._server = server
        self._call_id = call_id
        self._request_id = request_id
        self._text = text
        self.answers: list[dict[str, Any]] = []

    def run(self) -> None:
        self.answers.append(self._server.call("held", {"text": self._text}, self._call_id, self._request_id))


@pytest.fixture
def tools() -> Iterator[_Tools]:
    tools = _Tools()
    yield tools
    tools.release.set()


def _serve(journal: CallJournal, tools: _Tools) -> TestClient:
    server = _JournaledFastMCP(journal, name="test")
    server.add_tool(tools.held, name="held")
    server.add_tool(tools.fails, name="fails")
    server.add_tool(tools.call_id, name="call_id")
    return TestClient(server.streamable_http_app(), base_url="http://127.0.0.1:8000")


def _text(answer: dict[str, Any]) -> CallResult:
    """:return: a call's answer as the journal keeps it."""
    result = answer["result"]
    return CallResult("".join(item["text"] for item in result["content"]), result["isError"])


def test_a_journaled_call_is_answered_and_its_answer_journaled(tools: _Tools) -> None:
    tools.release.set()
    journal = CallJournal()
    with _serve(journal, tools) as client:
        answer = _Server(client).call("held", {"text": "x"}, "call-1")

    assert _text(answer) == CallResult("answered x")
    assert journal.resume("call-1") == CallResult("answered x")


def test_a_failed_call_is_journaled_as_the_failure_it_answered(tools: _Tools) -> None:
    journal = CallJournal()
    with _serve(journal, tools) as client:
        answer = _Server(client).call("fails", {}, "call-1")

    assert _text(answer).is_error
    assert journal.resume("call-1") == _text(answer)


def test_a_call_that_names_no_call_id_is_not_journaled(tools: _Tools, root: Path) -> None:
    tools.release.set()
    with _serve(CallJournal(), tools) as client:
        answer = _Server(client).call("held", {"text": "x"})

    assert _text(answer) == CallResult("answered x")
    assert list((root / supervise.OWNER / "call").glob("*.json")) == []


def test_a_call_id_that_cannot_name_an_entry_is_answered_with_an_error_and_its_work_is_not_run(tools: _Tools, root: Path) -> None:
    # the tool holds until released, which this test never does, so an answer means its work was not run
    with _serve(CallJournal(), tools) as client:
        answer = _text(_Server(client).call("held", {"text": "x"}, call_id="../x"))

    assert answer.is_error
    assert "../x" in answer.text
    assert list((root / supervise.OWNER / "call").glob("*.json")) == []


def test_a_call_the_listener_refused_is_not_journaled(tools: _Tools) -> None:
    journal = CallJournal()
    with _serve(journal, tools) as client:
        _Server(client)
        request = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "held", "arguments": {"text": "x"}}}
        refused = client.post("/mcp", json=request, headers={**_MCP_HEADERS, "Mcp-Session-Id": "unknown", CALL_ID_HEADER: "call-1"})

    assert refused.status_code == 404
    assert tools.runs == 0
    with pytest.raises(NoCallError):
        journal.resume("call-1")


def test_the_call_id_is_set_for_the_calls_work(tools: _Tools) -> None:
    with _serve(CallJournal(), tools) as client:
        answer = _Server(client).call("call_id", {}, "call-1")

    assert _text(answer) == CallResult("call-1")


def test_a_repeated_call_id_is_answered_with_the_journaled_answer_and_run_once(tools: _Tools) -> None:
    tools.release.set()
    with _serve(CallJournal(), tools) as client:
        server = _Server(client)
        first = server.call("held", {"text": "first"}, "call-1", request_id=7)
        repeated = server.call("held", {"text": "repeated"}, "call-1", request_id=8)

    assert tools.runs == 1
    assert _text(repeated) == _text(first) == CallResult("answered first")


def test_a_repeated_call_id_of_a_call_still_running_waits_for_its_answer_and_runs_once(tools: _Tools) -> None:
    journal = CallJournal()
    waits = _watched(journal)
    with _serve(journal, tools) as client:
        server = _Server(client)
        first = _Call(server, "call-1", request_id=7, text="first")
        first.start()
        tools.started.wait()

        # the repeat, sent while the first runs, waiting on the journal or run a second time
        repeated = _Call(server, "call-1", request_id=8, text="repeated")
        repeated.start()
        while not waits.waiting.is_set() and tools.runs < 2:
            waits.waiting.wait(0.01)

        tools.release.set()
        first.join()
        repeated.join()

    assert tools.runs == 1
    assert [_text(answer) for answer in repeated.answers] == [_text(answer) for answer in first.answers] == [CallResult("answered first")]


def test_a_repeated_call_id_an_ended_instance_answered_is_answered_from_the_journal_without_running(tools: _Tools) -> None:
    tools.release.set()
    _ended_instance({"call-1": "the journaled answer"})

    with _serve(CallJournal(), tools) as client:
        answer = _Server(client).call("held", {"text": "x"}, "call-1")

    assert tools.runs == 0
    assert _text(answer) == CallResult("the journaled answer")


def test_a_repeated_call_id_an_ended_instance_left_unanswered_is_answered_as_resume_answers_it(tools: _Tools) -> None:
    tools.release.set()
    _ended_instance({"call-1": None})

    journal = CallJournal()
    with _serve(journal, tools) as client:
        answer = _Server(client).call("held", {"text": "x"}, "call-1")

    assert tools.runs == 0
    assert _text(answer).is_error
    assert _text(answer) == journal.resume("call-1")


def test_a_call_cancelled_while_it_runs_is_journaled_as_a_failure(tools: _Tools) -> None:
    journal = CallJournal()
    with _serve(journal, tools) as client:
        server = _Server(client)
        call = _Call(server, "call-1", request_id=7)
        call.start()
        tools.started.wait()

        server.cancel(7)
        call.join()
        resume = _Resume(journal, "call-1")
        resume.start()
        resume.join(timeout=60)

    assert not resume.is_alive(), "the cancelled call was never journaled as answered"
    assert [answer.is_error for answer in resume.answers] == [True]
