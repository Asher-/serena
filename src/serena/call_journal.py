"""
Serena's journal of the calls it serves: one file per call under ``<root>/<owner>/call``, keyed by the call id the hop
before serena sends in X-Brain-Call-Id, so that the instance after a restart answers ``brain/resume`` for a call an ended
instance received. The journal holds an exclusive lock on ``<root>/<owner>/instance.lock`` until it is closed or its
process ends, and refuses to open while another live instance holds that lock. The owner is ``serena`` for the daemon on
:data:`DEFAULT_PORT` and for a serena listening on no port, and ``serena-<port>`` for a serena listening on any other port
(:func:`daemon_owner`).
"""

import fcntl
import json
import threading
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import psutil
from mcp.server.lowlevel.server import request_ctx
from mcp.types import CallToolRequest, CallToolResult, ServerResult, TextContent

from solidlsp.util import supervise

CALL_ID_HEADER = "X-Brain-Call-Id"
"""The header in which the hop before a server names a call."""

RESUME_METHOD = "brain/resume"
"""The method that asks a server for a call's answer."""

SERVER = "serena"
"""The name of the server, the owner of the records of the default daemon and of a serena listening on no port."""

DEFAULT_PORT = 9102
"""The port the default serena daemon listens on, the one the brain's config/brain-mcp-multiplexer.yml names for serena."""

NO_CALL_CODE = -32003
"""The JSON-RPC error code of the answer to a ``brain/resume`` naming a call the journal never received."""

CALL_ID_VAR: ContextVar[str | None] = supervise.CALL_ID
"""
The call id of the journaled ``tools/call`` whose work is running, set by :class:`JournaledCallHandler` while that work
runs. It is :data:`solidlsp.util.supervise.CALL_ID` itself.
"""

_POLL_SECONDS = 0.05
"""How long a ``resume`` of a call another live instance received waits before it reads the call's entry again."""

Receive = Callable[[], Awaitable[dict[str, Any]]]
"""An ASGI application's receive channel."""

Send = Callable[[dict[str, Any]], Awaitable[None]]
"""An ASGI application's send channel."""


def daemon_owner(port: int | None) -> str:
    """
    :param port: the port serena listens on, or None for a serena listening on none, as one serving stdio does.
    :return: the owner of the records of that serena: :data:`SERVER` on :data:`DEFAULT_PORT` or on no port, and
        :data:`SERVER` with the port appended on any other, so that a second serena listening, such as a manual run, is a
        server of its own whose processes and calls are kept apart from the daemon's
        (decision://brain/daemon_restart_survival/journal/instance).
    """
    if port is None or port == DEFAULT_PORT:
        return SERVER
    return f"{SERVER}-{port}"


class NoCallError(Exception):
    """The journal holds no call of the id asked for."""


class JournalHeldError(Exception):
    """Another live instance holds the journal's lock."""


@dataclass(frozen=True)
class CallResult:
    """What a call answered."""

    text: str
    """the text of the answer."""
    is_error: bool = False
    """whether the answer is a failure."""


@dataclass(frozen=True)
class _Instance:
    """A process, named by its pid and its kernel start time, as a journal entry names the instance that received it."""

    pid: int
    """the process id."""
    start: str
    """the process's start time, as psutil reports it."""

    @classmethod
    def this(cls) -> "_Instance":
        """
        :return: the process this code runs in.
        """
        process = psutil.Process()
        return cls(process.pid, str(process.create_time()))

    @classmethod
    def of(cls, entry: dict[str, Any]) -> "_Instance":
        """
        :param entry: a journal entry.
        :return: the instance that received the entry's call.
        """
        named = entry.get("instance") or {}
        return cls(int(named.get("pid", 0)), str(named.get("start", "")))

    def alive(self) -> bool:
        """
        :return: whether the process is running: a process of its pid exists, started when it did, and is no zombie.
        """
        try:
            process = psutil.Process(self.pid)
            return str(process.create_time()) == self.start and process.status() != psutil.STATUS_ZOMBIE
        except (psutil.Error, ValueError):
            return False


class CallJournal:
    """The calls this instance and the ones before it received, opened under the lock that admits one live instance."""

    def __init__(self) -> None:
        """
        :raises JournalHeldError: when another live instance holds the journal's lock.
        """
        owner = supervise.restart_root() / supervise.OWNER
        self._directory = owner / "call"
        self._directory.mkdir(parents=True, exist_ok=True)

        # the instance lock, which the kernel releases when this process ends however it ends
        lock_path = owner / "instance.lock"
        self._lock_file = lock_path.open("a")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            self._lock_file.close()
            raise JournalHeldError(f"another live instance of serena holds {lock_path}, so the calls under it are that instance's") from e

        self._lock = threading.Lock()
        self._finished = threading.Condition(self._lock)
        self._open: set[str] = set()
        self._self = _Instance.this()

    def close(self) -> None:
        """
        Releases the journal's lock, after which another instance may open the journal.
        """
        self._lock_file.close()

    def _path(self, call_id: str) -> Path:
        if call_id != Path(call_id).name or not call_id:
            raise NoCallError(call_id)
        return self._directory / f"{call_id}.json"

    def _write(self, entry: dict[str, Any]) -> None:
        path = self._path(entry["id"])
        temporary = path.with_name(f"{path.name}.tmp")
        temporary.write_text(json.dumps(entry))
        temporary.replace(path)

    def receive(self, call_id: str, tool: str, arguments: Any) -> bool:
        """
        Records a call before its work starts, unless the journal already holds the call id, whose entry is then left
        as it is.

        :param call_id: the id the hop before this server sent in X-Brain-Call-Id.
        :param tool: the tool called.
        :param arguments: the call's arguments.
        :return: whether the call was recorded, and so is to be run.
        :raises NoCallError: when the call id cannot name an entry of the journal.
        """
        with self._lock:
            path = self._path(call_id)
            if call_id in self._open or path.exists():
                return False
            instance = {"pid": self._self.pid, "start": self._self.start}
            self._write(
                {"id": call_id, "tool": tool, "arguments": arguments, "received": datetime.now(UTC).isoformat(), "instance": instance}
            )
            self._open.add(call_id)
            return True

    def claim(self, call_id: str) -> None:
        """
        Makes this instance the one that answers a call an ended instance received, leaving the call's entry as it is: a
        ``resume`` of the call waits for this instance's ``finish`` of it.

        :param call_id: the call.
        """
        with self._lock:
            self._open.add(call_id)

    def finish(self, call_id: str, result: CallResult) -> None:
        """
        Records a call's answer, unless the call already holds one, which stands because its caller may have read it, and
        releases every ``brain/resume`` waiting on the call.

        :param call_id: the call.
        :param result: its answer.
        """
        with self._lock:
            entry = json.loads(self._path(call_id).read_text())
            if "result" not in entry:
                entry["result"] = {"text": result.text, "is_error": result.is_error}
                entry["finished"] = datetime.now(UTC).isoformat()
                self._write(entry)
            self._open.discard(call_id)
            self._finished.notify_all()

    def resume(self, call_id: str) -> CallResult:
        """
        :param call_id: the call.
        :return: the call's answer: the one journaled; once it is answered, for a call this instance answers or another
            live instance received; otherwise, for a call whose instance ended before answering it, a failure naming the
            restart, journaled as the call's answer.
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
                if call_id in self._open:
                    self._finished.wait()
                    continue
                holder = _Instance.of(entry)
                if holder != self._self and holder.alive():
                    self._finished.wait(_POLL_SECONDS)
                    continue
                break

            # the call's work ran only in an instance that is gone, so nothing will answer it
            text = f"Error: serena restarted while call {call_id} ({entry.get('tool', '')}) ran; its work was lost"
            entry["result"] = {"text": text, "is_error": True}
            entry["finished"] = datetime.now(UTC).isoformat()
            self._write(entry)
            return CallResult(text, True)


class JournaledCallHandler:
    """
    FastMCP's handler of ``tools/call`` requests, wrapped so that a call whose HTTP request names a call id in
    X-Brain-Call-Id is journaled before its work starts and its answer is journaled where its work ends, in FastMCP's
    session task, which the end of the request's connection does not end. A call id the journal already holds is not run
    again: the call is answered as ``brain/resume`` answers it.
    """

    def __init__(self, handler: Callable[[CallToolRequest], Awaitable[ServerResult]], journal: CallJournal) -> None:
        """
        :param handler: the handler FastMCP registered for ``tools/call``.
        :param journal: the journal.
        """
        self._handler = handler
        self._journal = journal

    async def __call__(self, request: CallToolRequest) -> ServerResult:
        # the call id the HTTP request carrying the call names
        call_id = _call_id()
        if not call_id:
            return await self._handler(request)

        # the call, recorded before its work starts, or answered from the journal when the journal already holds it
        tool = request.params.name
        try:
            fresh = self._journal.receive(call_id, tool, request.params.arguments)
        except NoCallError:
            return _tool_result(CallResult(f"Error: {CALL_ID_HEADER} {call_id!r} cannot name a call of serena's journal", True))
        if not fresh:
            return _tool_result(await anyio.to_thread.run_sync(self._journal.resume, call_id, abandon_on_cancel=True))

        # the call's work, whose answer is journaled where it ends, however it ends
        token = CALL_ID_VAR.set(call_id)
        try:
            response = await self._handler(request)
        except BaseException as e:
            self._journal.finish(call_id, CallResult(f"Error: call {call_id} ({tool}) ended before it answered: {type(e).__name__}", True))
            raise
        finally:
            CALL_ID_VAR.reset(token)
        self._journal.finish(call_id, _journaled(response))
        return response


class CallJournalLayer:
    """
    The ASGI layer around serena's streamable-http listener that answers ``brain/resume``, which FastMCP, refusing a
    method it does not know, cannot. Every other request is passed on to the listener as it came.
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

        # a resume answered here, and every other request passed on
        message = _decode(body)
        if message.get("method") == RESUME_METHOD:
            await self._answer_resume(message, send)
            return
        await self._app(scope, replay, send)

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
            error = {"code": NO_CALL_CODE, "message": f"the journal of serena holds no call {call_id}"}
            await _respond(send, {"jsonrpc": "2.0", "id": request_id, "error": error})
            return
        content = [{"type": "text", "text": result.text}]
        await _respond(send, {"jsonrpc": "2.0", "id": request_id, "result": {"content": content, "isError": result.is_error}})


def _call_id() -> str:
    """
    :return: the call id in X-Brain-Call-Id of the HTTP request that carried the request being handled, or "" when there
        is no such request or it names none.
    """
    try:
        context = request_ctx.get()
    except LookupError:
        return ""
    headers = getattr(context.request, "headers", None)
    return headers.get(CALL_ID_HEADER, "") if headers is not None else ""


def _journaled(response: ServerResult) -> CallResult:
    """
    :param response: the answer FastMCP's handler gave a ``tools/call``.
    :return: the answer as the journal keeps it: the text of its content and whether it is a failure.
    """
    result = response.root
    text = "".join(getattr(item, "text", "") for item in getattr(result, "content", []))
    return CallResult(text, bool(getattr(result, "isError", False)))


def _tool_result(result: CallResult) -> ServerResult:
    """
    :param result: an answer the journal keeps.
    :return: the answer as a ``tools/call`` result.
    """
    return ServerResult(CallToolResult(content=[TextContent(type="text", text=result.text)], isError=result.is_error))


def _decode(body: bytes) -> dict[str, Any]:
    try:
        message = json.loads(body)
    except ValueError:
        return {}
    return message if isinstance(message, dict) else {}


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
