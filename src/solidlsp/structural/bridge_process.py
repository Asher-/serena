"""
A structural backend's bridge: a long-lived supervised process answering length-prefixed JSON requests. A bridge
outlives serena, so the next instance adopts a live one instead of starting another beside it
(pg://brain/daemon/restart/readoption). Its stdin is a FIFO that gives it no end of file
(pg://brain/daemon/restart/stream), so a stop serena means reaches it by signal
(pg://brain/daemon/restart/cancellation).
"""

import hashlib
import json
import logging
import os
import struct
import subprocess
import threading
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from secrets import token_hex
from typing import Any

from solidlsp.structural.errors import ParseError
from solidlsp.util import supervise

log = logging.getLogger(__name__)

_LENGTH = struct.Struct("<I")
"""The prefix of every frame, in both directions."""

_STOP_SECONDS = 2.0
"""How long a bridge has to end on its terminate signal before it is killed."""

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
class BridgeState:
    """What the next instance needs to take a bridge over."""

    record: str
    """the path of the bridge's supervise record."""
    instance_pid: int
    """the pid of the serena instance holding the bridge."""


class BridgeStates:
    """The state files of the bridges that run one command. One command runs in as many bridges as serena has sessions."""

    def __init__(self, command: Sequence[str]) -> None:
        """
        :param command: the command the bridges run.
        """
        key = hashlib.sha256(json.dumps(list(command)).encode()).hexdigest()[:32]
        self._directory = supervise.restart_root() / supervise.OWNER / "bridge" / key

    def claim(self) -> BridgeState | None:
        """
        :return: the state of a bridge whose last instance has ended, now held by this instance, or None when there
            is none. A bridge another live instance holds is that instance's.
        """
        for path in sorted(self._directory.glob("*.json")):
            state = self._claimed(path)
            if state is not None:
                return state
        return None

    def _claimed(self, path: Path) -> BridgeState | None:
        state = self._load(path)
        if state is None or self._held(state):
            return None

        # the rename takes the file from every other instance while this one looks again: an instance that finished
        # claiming before the rename has written the file back under its own pid, so it is read once more here
        claim = path.with_name(f"{path.name}.{os.getpid()}.claim")
        try:
            path.rename(claim)
        except FileNotFoundError:
            return None
        current = self._load(claim)
        if current is None or self._held(current):
            claim.rename(path)
            return None

        current.instance_pid = os.getpid()
        self.save(current)
        claim.unlink()
        return current

    @staticmethod
    def _load(path: Path) -> BridgeState | None:
        try:
            return BridgeState(**json.loads(path.read_text()))
        except (FileNotFoundError, ValueError, TypeError):
            return None

    @staticmethod
    def _held(state: BridgeState) -> bool:
        return state.instance_pid == os.getpid() or supervise.process_exists(state.instance_pid)

    def save(self, state: BridgeState) -> None:
        """
        :param state: the state to hold, written by rename so a reader sees one whole state.
        """
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._path(state)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(asdict(state)))
        temporary.replace(path)

    def remove(self, state: BridgeState) -> None:
        """
        :param state: the state of a bridge that is gone.
        """
        self._path(state).unlink(missing_ok=True)

    def _path(self, state: BridgeState) -> Path:
        return self._directory / f"{Path(state.record).stem}.json"


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
        process = supervise.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            waiting={"kind": "solidlsp.structural_bridge"},
        )
        self._state = BridgeState(record=str(process.record), instance_pid=os.getpid())
        self._states.save(self._state)
        return process

    def _adopt(self) -> supervise.Popen | None:
        """
        :return: a live bridge an ended instance left, now this instance's, or None when no such bridge answers.
        """
        while (state := self._states.claim()) is not None:
            try:
                process = supervise.Popen.adopt(Path(state.record))
            except FileNotFoundError:
                self._states.remove(state)
                continue
            if process.poll() is None and self._synchronize(process):
                log.info("Adopted %s bridge %s", self._language_key, state.record)
                self._state = state
                return process
            self._stop(process, state)
        return None

    def _synchronize(self, process: supervise.Popen) -> bool:
        """
        Sends a request of a name no bridge knows, whose answer repeats the name, and consumes stdout from its first
        byte through that answer. Whatever the ended instance sent has then been answered and passed over, so the next
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

    @staticmethod
    def _receive(process: supervise.Popen) -> bytes:
        assert process.stdout is not None
        (length,) = _LENGTH.unpack(_read_exact(process.stdout, _LENGTH.size))
        return _read_exact(process.stdout, length)


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
