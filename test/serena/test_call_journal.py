"""
The call journal: ``brain/resume`` answers a finished call with its result, and a call whose instance
ended with the failure naming the restart, exactly as the same answer each time it is asked.
"""

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from httpx import Response
from starlette.testclient import TestClient

from serena.call_journal import CALL_ID_HEADER, RESUME_METHOD, CallJournal, CallJournalLayer, CallResult, NoCallError
from solidlsp.util import supervise


@pytest.fixture(autouse=True)
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(supervise.ROOT_ENV, str(tmp_path))
    return tmp_path


def test_a_finished_call_is_answered_with_its_result_by_the_next_instance() -> None:
    first = CallJournal()
    first.receive("call-1", "find_symbol", {"name": "x"})
    first.finish("call-1", CallResult("found"))
    assert CallJournal().resume("call-1") == CallResult("found")


def test_a_call_in_flight_in_this_instance_is_answered_when_it_finishes() -> None:
    journal = CallJournal()
    journal.receive("call-1", "find_symbol", {})
    answers: list[CallResult] = []
    waiter = threading.Thread(target=lambda: answers.append(journal.resume("call-1")))
    waiter.start()
    journal.finish("call-1", CallResult("found"))
    waiter.join()
    assert answers == [CallResult("found")]


def test_a_call_lost_with_its_instance_is_answered_with_the_restart_and_the_same_answer_again() -> None:
    CallJournal().receive("call-1", "find_symbol", {})
    next_instance = CallJournal()
    lost = next_instance.resume("call-1")
    assert lost.is_error
    assert "restarted" in lost.text
    assert next_instance.resume("call-1") == lost


@pytest.mark.parametrize("call_id", ["absent", "../absent", ""])
def test_a_call_the_journal_does_not_hold_is_refused(call_id: str) -> None:
    with pytest.raises(NoCallError):
        CallJournal().resume(call_id)


_ANSWER = {"jsonrpc": "2.0", "id": 7, "result": {"content": [{"type": "text", "text": "the answer"}], "isError": False}}
_FAILURE = {"jsonrpc": "2.0", "id": 7, "error": {"code": -32603, "message": "it broke"}}
_NOTIFICATION = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}


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


def _client(listener: _Listener) -> tuple[TestClient, CallJournal]:
    journal = CallJournal()
    return TestClient(CallJournalLayer(listener, journal, "/mcp")), journal


def _call(client: TestClient, call_id: str | None = "call-1") -> Response:
    headers = {CALL_ID_HEADER: call_id} if call_id else {}
    request = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "the_tool", "arguments": {"a": 1}}}
    return client.post("/mcp", json=request, headers=headers)


def _resume(client: TestClient, params: dict[str, Any]) -> dict[str, Any]:
    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 9, "method": RESUME_METHOD, "params": params})
    assert response.status_code == 200
    return response.json()


def test_a_call_is_passed_on_whole_and_its_answer_reaches_the_caller_and_the_journal() -> None:
    listener = _Listener(json.dumps(_ANSWER).encode())
    client, journal = _client(listener)

    response = _call(client)

    assert response.json() == _ANSWER
    assert json.loads(listener.seen[0])["params"]["name"] == "the_tool"
    assert journal.resume("call-1") == CallResult("the answer")


def test_an_answer_in_a_stream_of_events_is_taken_from_the_event_that_answers_the_call() -> None:
    events = b"".join(b"event: message\r\ndata: " + json.dumps(m).encode() + b"\r\n\r\n" for m in (_NOTIFICATION, _ANSWER))
    client, journal = _client(_Listener(b": ping\r\n\r\n" + events))

    _call(client)

    assert journal.resume("call-1") == CallResult("the answer")


def test_a_failed_call_is_journaled_as_a_failure() -> None:
    client, journal = _client(_Listener(json.dumps(_FAILURE).encode()))

    _call(client)

    assert journal.resume("call-1") == CallResult("Error: it broke", True)


def test_a_call_the_listener_refused_was_never_received() -> None:
    client, journal = _client(_Listener(b"", status=404))

    _call(client)

    with pytest.raises(NoCallError):
        journal.resume("call-1")


def test_a_call_with_no_id_is_passed_on_and_not_journaled(root: Path) -> None:
    client, _journal = _client(_Listener(json.dumps(_ANSWER).encode()))

    assert _call(client, call_id=None).json() == _ANSWER

    assert list((root / supervise.OWNER / "call").glob("*.json")) == []


def test_another_route_is_passed_on_untouched() -> None:
    listener = _Listener(b"ok")
    client, _journal = _client(listener)

    assert client.post("/session/end", content=b"anything").text == "ok"
    assert listener.seen == [b"anything"]


def test_resume_answers_a_call_of_the_ended_instance_in_the_form_of_a_tool_result() -> None:
    client, _journal = _client(_Listener(json.dumps(_ANSWER).encode()))
    _call(client)
    next_client, _next_journal = _client(_Listener(b"never reached"))

    answer = _resume(next_client, {"call_id": "call-1"})

    assert answer == {"jsonrpc": "2.0", "id": 9, "result": {"content": [{"type": "text", "text": "the answer"}], "isError": False}}


def test_resume_answers_a_call_lost_with_its_instance_with_the_restart() -> None:
    CallJournal().receive("call-1", "the_tool", {})
    client, _journal = _client(_Listener(b"never reached"))

    result = _resume(client, {"call_id": "call-1"})["result"]

    assert result["isError"] is True
    assert "restarted" in result["content"][0]["text"]


def test_resume_of_a_call_the_journal_does_not_hold_is_a_failed_result() -> None:
    client, _journal = _client(_Listener(b"never reached"))

    result = _resume(client, {"call_id": "absent"})["result"]

    assert result["isError"] is True
    assert "absent" in result["content"][0]["text"]


@pytest.mark.parametrize("params", [{}, {"call_id": ""}, {"call_id": 5}])
def test_resume_that_names_no_call_is_refused_as_invalid(params: dict[str, Any]) -> None:
    client, _journal = _client(_Listener(b"never reached"))

    assert _resume(client, params)["error"]["code"] == -32602
