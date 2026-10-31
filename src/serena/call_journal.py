"""
Serena's journal of the calls it serves, in the form every brain server keeps
(``platform/shared-core/calls``): one file per call under ``<root>/serena/call``, so that the instance
after a restart answers ``brain/resume`` for a call the ended one received.
"""

import json
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import psutil

from solidlsp.util import supervise

CALL_ID_HEADER = "X-Brain-Call-Id"
"""The header in which the hop before a server names a call."""

RESUME_METHOD = "brain/resume"
"""The method that asks a server for a call's answer."""

Receive = Callable[[], Awaitable[dict[str, Any]]]
"""An ASGI application's receive channel."""

Send = Callable[[dict[str, Any]], Awaitable[None]]
"""An ASGI application's send channel."""


class NoCallError(Exception):
    """The journal holds no call of the id asked for."""


@dataclass(frozen=True)
class CallResult:
    """What a call answered."""

    text: str
    """the text of the answer."""
    is_error: bool = False
    """whether the answer is a failure."""


class CallJournal:
    """The calls this instance and the ones before it received."""

    def __init__(self) -> None:
        self._directory = supervise.restart_root() / supervise.OWNER / "call"
        self._directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._finished = threading.Condition(self._lock)
        self._open: set[str] = set()
        process = psutil.Process()
        self._self = {"pid": process.pid, "start": str(process.create_time())}

    def _path(self, call_id: str) -> Path:
        if call_id != Path(call_id).name or not call_id:
            raise NoCallError(call_id)
        return self._directory / f"{call_id}.json"

    def _write(self, entry: dict[str, Any]) -> None:
        path = self._path(entry["id"])
        temporary = path.with_name(f"{path.name}.tmp")
        temporary.write_text(json.dumps(entry))
        temporary.replace(path)

    def receive(self, call_id: str, tool: str, arguments: Any) -> None:
        """
        Records a call before its work starts.

        :param call_id: the id the hop before this server sent in X-Brain-Call-Id.
        :param tool: the tool called.
        :param arguments: the call's arguments.
        """
        with self._lock:
            self._write(
                {
                    "id": call_id,
                    "tool": tool,
                    "arguments": arguments,
                    "received": datetime.now(UTC).isoformat(),
                    "instance": self._self,
                }
            )
            self._open.add(call_id)

    def finish(self, call_id: str, result: CallResult) -> None:
        """
        Records a call's answer and releases every ``brain/resume`` waiting on it.

        :param call_id: the call.
        :param result: its answer.
        """
        with self._lock:
            entry = json.loads(self._path(call_id).read_text())
            entry["result"] = {"text": result.text, "is_error": result.is_error}
            entry["finished"] = datetime.now(UTC).isoformat()
            self._write(entry)
            self._open.discard(call_id)
            self._finished.notify_all()

    def resume(self, call_id: str) -> CallResult:
        """
        :param call_id: the call.
        :return: the call's answer: the one journaled, the one a call this instance holds comes to, or,
            for a call whose work ran only inside an instance that ended, a failure naming the restart.
        :raises NoCallError: when the journal holds no such call.
        """
        with self._lock:
            while True:
                try:
                    entry = json.loads(self._path(call_id).read_text())
                except FileNotFoundError as e:
                    raise NoCallError(call_id) from e
                if entry.get("result"):
                    return CallResult(entry["result"]["text"], entry["result"].get("is_error", False))
                if call_id not in self._open:
                    break
                self._finished.wait()

            # the call's work ran only in an instance that is gone, so nothing will answer it
            text = f"Error: serena restarted while call {call_id} ({entry.get('tool', '')}) ran; its work was lost"
            entry["result"] = {"text": text, "is_error": True}
            entry["finished"] = datetime.now(UTC).isoformat()
            self._write(entry)
            return CallResult(text, True)

    def prune(self, older_than_seconds: float) -> None:
        """
        Removes the entries of calls answered long enough ago that no hop will ask again.

        :param older_than_seconds: the age past which an answered entry goes.
        """
        horizon = time.time() - older_than_seconds
        with self._lock:
            for path in self._directory.glob("*.json"):
                if path.stat().st_mtime < horizon and path.stem not in self._open:
                    path.unlink(missing_ok=True)

    def discard(self, call_id: str) -> None:
        """
        Removes a call the listener refused, whose work never started: to a later ``brain/resume`` it was never received.

        :param call_id: the call.
        """
        with self._lock:
            self._path(call_id).unlink(missing_ok=True)
            self._open.discard(call_id)
            self._finished.notify_all()


class CallJournalLayer:
    """
    The ASGI layer around serena's streamable-http listener: it journals each ``tools/call`` the hop before serena named
    in X-Brain-Call-Id, and it answers ``brain/resume``, which FastMCP, refusing a method it does not know, cannot.
    """

    def __init__(self, app: Any, journal: CallJournal, path: str) -> None:
        """
        :param app: the listener's ASGI application.
        :param journal: the journal.
        :param path: the listener's MCP endpoint.
        """
        self._app = app
        self._journal = journal
        self._path = path

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != self._path:
            await self._app(scope, receive, send)
            return

        # the body, read once here and replayed to the listener
        body = await _read_body(receive)
        delivered = False

        async def replay() -> dict[str, Any]:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        # what the request is, as far as it matters here
        message = _decode(body)
        method = message.get("method")
        if method == RESUME_METHOD:
            await self._answer_resume(message, send)
            return
        call_id = _header(scope, CALL_ID_HEADER)
        if method != "tools/call" or not call_id:
            await self._app(scope, replay, send)
            return

        # the call, journaled before its work starts and answered from what the listener responds
        params = message.get("params") or {}
        self._journal.receive(call_id, params.get("name", ""), params.get("arguments"))
        response = _CapturedResponse(send, self._journal, call_id, message.get("id"))
        try:
            await self._app(scope, replay, response.send)
        finally:
            if not response.answered:
                self._journal.discard(call_id)

    async def _answer_resume(self, message: dict[str, Any], send: Send) -> None:
        request_id = message.get("id")
        call_id = (message.get("params") or {}).get("call_id")
        if not call_id or not isinstance(call_id, str):
            error = {"code": -32602, "message": f"{RESUME_METHOD}: params must name a call_id"}
            await _respond(send, {"jsonrpc": "2.0", "id": request_id, "error": error})
            return
        try:
            result = await anyio.to_thread.run_sync(self._journal.resume, call_id, abandon_on_cancel=True)
        except NoCallError:
            result = CallResult(f"Error: the journal of serena holds no call {call_id}", True)
        content = [{"type": "text", "text": result.text}]
        await _respond(send, {"jsonrpc": "2.0", "id": request_id, "result": {"content": content, "isError": result.is_error}})


class _CapturedResponse:
    """
    The listener's response to a call, passed on as it comes. The answer is journaled before the part holding it is
    passed on, so a caller never holds an answer the journal would call lost.
    """

    def __init__(self, send: Send, journal: CallJournal, call_id: str, request_id: Any) -> None:
        """
        :param send: the send channel of the request.
        :param journal: the journal holding the call.
        :param call_id: the call.
        :param request_id: the JSON-RPC id of the call.
        """
        self._send = send
        self._journal = journal
        self._call_id = call_id
        self._request_id = request_id
        self._body = bytearray()
        self.answered = False

    async def send(self, event: dict[str, Any]) -> None:
        if event["type"] == "http.response.body" and not self.answered:
            self._body.extend(event.get("body", b""))
            result = self._result()
            if result is not None:
                self._journal.finish(self._call_id, result)
                self.answered = True
        await self._send(event)

    def _result(self) -> CallResult | None:
        """
        :return: the answer the response has carried so far, or None when it has carried none, as one the listener
            refused never does.
        """
        for message in _messages(bytes(self._body)):
            if message.get("id") != self._request_id:
                continue
            if "error" in message:
                return CallResult(f"Error: {message['error'].get('message', '')}", True)
            result = message.get("result") or {}
            text = "".join(item.get("text", "") for item in result.get("content", []))
            return CallResult(text, bool(result.get("isError")))
        return None


def _messages(body: bytes) -> Iterator[dict[str, Any]]:
    """
    :param body: a response body, one JSON-RPC message or a stream of server-sent events.
    :return: the JSON-RPC messages it holds.
    """
    if body.lstrip().startswith(b"{"):
        yield _decode(body)
        return
    for line in body.splitlines():
        if line.startswith(b"data:"):
            yield _decode(line[len(b"data:") :])


def _decode(body: bytes) -> dict[str, Any]:
    try:
        message = json.loads(body)
    except ValueError:
        return {}
    return message if isinstance(message, dict) else {}


def _header(scope: dict[str, Any], name: str) -> str:
    encoded = name.lower().encode()
    for key, value in scope.get("headers", []):
        if key.lower() == encoded:
            return value.decode("latin-1")
    return ""


async def _read_body(receive: Receive) -> bytes:
    body = bytearray()
    while True:
        event = await receive()
        if event["type"] != "http.request":
            return bytes(body)
        body.extend(event.get("body", b""))
        if not event.get("more_body"):
            return bytes(body)


async def _respond(send: Send, message: dict[str, Any]) -> None:
    body = json.dumps(message).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    await send({"type": "http.response.body", "body": body})
