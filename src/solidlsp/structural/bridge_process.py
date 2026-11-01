"""
A structural backend's bridge: a long-lived supervised process answering length-prefixed JSON requests. A bridge
outlives serena, so the next instance adopts a live one instead of starting another beside it
(pg://brain/daemon/restart/readoption). Its stdin is a FIFO that gives it no end of file
(pg://brain/daemon/restart/stream), so a stop serena means reaches it by signal
(pg://brain/daemon/restart/cancellation).
"""

import json
import logging
import struct
import subprocess
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from secrets import token_hex
from typing import Any

from solidlsp.ls_survival import HOLDINGS, KeptState, KeptStates
from solidlsp.structural.errors import ParseError
from solidlsp.util import supervise

log = logging.getLogger(__name__)

_LENGTH = struct.Struct("<I")
"""The prefix of every frame, in both directions."""

_STOP_SECONDS = 2.0
"""How long a bridge has to end on its terminate signal before it is killed."""

BRIDGE_KIND = "solidlsp.structural_bridge"
"""The waiting kind of a bridge's record."""

_ADOPTION_SECONDS = 60.0
"""
How long an adopted bridge has to answer. It may first be finishing a request an ended instance sent; one that does
not answer is reading the rest of a request that instance left half written, and will never answer.
"""


def frame(body: bytes) -> bytes:
    """
    :param body: the encoded message.
    :return: the message as a bridge reads and writes it.
    """
    return _LENGTH.pack(len(body)) + body


@dataclass
class BridgeState(KeptState):
    """What the next instance needs to take a bridge over."""

    stdout_offset: int = 0
    """an offset of the bridge's stdout at a frame's first byte, below which every byte has been consumed."""


class BridgeStates(KeptStates[BridgeState]):
    """The state files of the bridges that run one command. One command runs in as many bridges as serena has sessions."""

    KIND = "bridge"
    STATE_TYPE = BridgeState

    def __init__(self, command: Sequence[str]) -> None:
        """
        :param command: the command the bridges run.
        """
        super().__init__(list(command))


class StructuralBridge:
    """
    One structural backend instance's bridge. It starts, or adopts, at the first call. Calls are serialized by a mutex
    so concurrent callers do not interleave their frames.
    """

    def __init__(self, command: Sequence[str], language_key: str) -> None:
        """
        :param command: the command that starts the bridge.
        :param language_key: the language named by a :class:`ParseError` when the bridge fails.
        """
        self._command = [str(argument) for argument in command]
        self._language_key = language_key
        self._states = BridgeStates(self._command)
        self._process: supervise.Popen | None = None
        self._state: BridgeState | None = None
        self._consumed = 0
        self._lock = threading.Lock()

    def call(self, op: str, **payload: Any) -> dict[str, Any]:
        """
        :param op: the operation.
        :param payload: the operation's fields.
        :return: the decoded response.
        """
        with self._lock:
            process = self._ensure_started()
            try:
                self._send(process, {"op": op, **payload})
                response = self._receive(process)
            except OSError as exc:
                stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
                raise ParseError(
                    language_key=self._language_key,
                    source_preview="",
                    detail=f"bridge communication failed: {exc}; stderr:\n{stderr}",
                ) from exc
            self._reclaim(process)
            return json.loads(response)

    def close(self) -> None:
        """Stops the bridge. Idempotent. Nothing here runs when a signal ends serena, so a restart stops no bridge."""
        with self._lock:
            process, self._process = self._process, None
            state, self._state = self._state, None
            if process is None or state is None:
                return
            self._stop(process, state)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _ensure_started(self) -> supervise.Popen:
        # lazy start; replace a bridge that has ended
        if self._process is not None and self._process.poll() is not None:
            assert self._state is not None
            self._stop(self._process, self._state)
            self._process = None
        if self._process is None:
            self._process = self._adopt() or self._start()
        return self._process

    def _start(self) -> supervise.Popen:
        # a bridge outlives the call that starts it, and so names none
        process = supervise.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            call_id="",
            waiting=supervise.Waiting.of(BRIDGE_KIND),
            keep=True,
        )
        self._state = BridgeState(record=str(process.record), instance_pid=0)
        self._state.hold()
        self._states.save(self._state)
        self._consumed = 0
        return process

    def _adopt(self) -> supervise.Popen | None:
        """
        :return: a live bridge this instance took over when it started or an ended instance left, now this instance's,
            or None when no such bridge answers.
        """
        while (state := HOLDINGS.take(self._states) or self._states.take_over()) is not None:
            try:
                process = supervise.Popen.adopt(Path(state.record), state.stdout_offset)
            except FileNotFoundError:
                self._states.remove(state)
                continue
            self._consumed = state.stdout_offset
            if process.poll() is None and self._synchronize(process):
                log.info("Adopted %s bridge %s", self._language_key, state.record)
                self._state = state
                return process
            self._stop(process, state)
        return None

    def _synchronize(self, process: supervise.Popen) -> bool:
        """
        Sends a request of a name no bridge knows, whose answer repeats the name, and consumes stdout from the offset the
        state names through that answer. Whatever the ended instance sent has then been answered and passed over, so the next
        call reads its own answer, and nothing about a call had to be stored while the ended instance ran.

        :param process: an adopted bridge.
        :return: whether the bridge answered in time.
        """
        name = f"adoption-{token_hex(8)}".encode()
        answered = threading.Event()

        def read() -> None:
            try:
                while name not in self._receive(process):
                    pass
                answered.set()
            except OSError:
                pass

        reader = threading.Thread(target=read, name="bridge-adoption", daemon=True)
        reader.start()
        self._send(process, {"op": name.decode()})
        reader.join(_ADOPTION_SECONDS)
        return answered.is_set()

    def _stop(self, process: supervise.Popen, state: BridgeState) -> None:
        process.terminate()
        try:
            process.wait(timeout=_STOP_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.close_streams()
        process.release()
        self._states.remove(state)

    @staticmethod
    def _send(process: supervise.Popen, request: dict[str, Any]) -> None:
        assert process.stdin is not None
        process.stdin.write(frame(json.dumps(request).encode("utf-8")))
        process.stdin.flush()

    def _receive(self, process: supervise.Popen) -> bytes:
        assert process.stdout is not None
        (length,) = _LENGTH.unpack(_read_exact(process.stdout, _LENGTH.size))
        body = _read_exact(process.stdout, length)
        self._consumed += _LENGTH.size + length
        return body

    def _reclaim(self, process: supervise.Popen) -> None:
        """
        Frees the blocks of stdout the bridge's answers have been read from, once they reach a megabyte past the offset
        the state names. The state names the offset first, so a next instance reads on from a frame's first byte above
        every freed block (bug://brain/multiplexer/stdio/stdout/file/growth).

        :param process: the bridge.
        """
        assert self._state is not None
        if self._consumed - self._state.stdout_offset >= supervise.RECLAIM_BYTES:
            self._state.stdout_offset = self._consumed
            self._states.save(self._state)
            process.reclaim_stdout(self._consumed)


def _read_exact(stream: Any, n: int) -> bytes:
    chunks: list[bytes] = []
    remaining = n
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            raise OSError("bridge closed stdout unexpectedly")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
