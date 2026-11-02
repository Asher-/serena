"""
Every process serena starts, started through the brain-supervise program.

A supervised process leads a process group of its own under a supervisor that leads a session of
its own, writes its streams to files and FIFOs rather than to pipes serena holds, and has its outcome
recorded whether or not serena is running, so a language server or bridge outlives a SIGKILL of
serena's process group or a SIGTERM of serena
(pg://brain/daemon/restart/survival). :class:`Popen` and :func:`run` stand where ``subprocess.Popen``
and ``subprocess.run`` stood; ``subprocess.PIPE``, ``subprocess.DEVNULL``, ``subprocess.STDOUT`` and its
exceptions keep their meanings. This module is the one place serena calls ``subprocess`` to start
anything, and what it starts is the supervisor; the one start elsewhere is the command-line editor of
serena/cli.py, exempted by decision://brain/daemon_restart_survival/serena/cli/editor.
"""

import contextvars
import ctypes
import errno
import fcntl
import io
import json
import logging
import os
import select
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from secrets import token_hex
from typing import IO, Any, Protocol

import psutil

log = logging.getLogger(__name__)

OWNER = "serena"
"""The server whose records these are: they live under ``<root>/serena/process``."""

PROGRAM_ENV = "BRAIN_SUPERVISE"
"""The environment variable naming the brain-supervise program; PATH is searched without it."""

ROOT_ENV = "BRAIN_RESTART_ROOT"
"""The environment variable naming the directory that holds every server's records."""

CALL_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("solidlsp_supervise_call_id", default=None)
"""
The call on whose behalf the running code starts processes, set while serena serves a journaled call. A :class:`Popen`
that names no call records this one, so every process started for a call names it (pg://brain/daemon/restart/record).
"""

INLINE_KIND = "solidlsp.inline"
"""
The waiting kind of a process a call waits on inline and that names no continuation of its own: serena's next instance
waits on it, discards it and answers its call with the restart failure (decision://brain/daemon_restart_survival/journal/continuation).
"""

RECLAIM_BYTES = 1 << 20
"""How much of a stream file a reader consumes before it frees the blocks holding what it consumed."""

_END_CHECK_SECONDS = 0.05
"""How long a reader waits for a file to grow before it reads the record again."""

_READ_ONLY_STDIN = 'exec 3<>"$0" <"$0"; exec "$@"'
"""
The shell program that runs a command with its FIFO, $0, opened read-write on descriptor 3 and read-only
on descriptor 0. On macOS FIONREAD answers 0 for a FIFO opened read-write, so a program that sizes its
reads with it never reads that descriptor. The process still holds a writer of its own on descriptor 3,
so it reads no end of file when serena ends, and ``exec`` keeps the pid the supervisor recorded.
"""

_SIGNALS = {
    "hangup": signal.SIGHUP,
    "interrupt": signal.SIGINT,
    "quit": signal.SIGQUIT,
    "illegal instruction": signal.SIGILL,
    "abort trap": signal.SIGABRT,
    "aborted": signal.SIGABRT,
    "killed": signal.SIGKILL,
    "bus error": signal.SIGBUS,
    "segmentation fault": signal.SIGSEGV,
    "broken pipe": signal.SIGPIPE,
    "terminated": signal.SIGTERM,
}
"""The signal numbers of the names a record holds, which are the names Go gives signals."""

WATCH_SECONDS = 0.5
"""How long a watcher of a process another reader does not follow waits before it reads the record again."""

_STOP_SECONDS = 2.0
"""How long a process has to end on its terminate signal before it is killed."""

_F_PUNCHHOLE = 99
"""The fcntl command that frees the blocks of a range of a file on macOS, from <sys/fcntl.h>."""

_FALLOC_PUNCH_HOLE = 0x02 | 0x01
"""The fallocate mode that frees the blocks of a range of a file on Linux and keeps its size, from <linux/falloc.h>."""


def restart_root() -> Path:
    """
    :return: the directory that holds every server's records, ``~/.brain/restart`` unless overridden.
    """
    return Path(os.environ.get(ROOT_ENV) or Path.home() / ".brain" / "restart")


def program() -> str:
    """
    :return: the path of the brain-supervise program.
    :raises FileNotFoundError: when it is neither named by :data:`PROGRAM_ENV` nor on PATH.
    """
    found = os.environ.get(PROGRAM_ENV) or shutil.which("brain-supervise")
    if not found:
        raise FileNotFoundError(errno.ENOENT, f"brain-supervise not found: set {PROGRAM_ENV} or put it on PATH")
    return found


@dataclass(frozen=True)
class Outcome:
    """How a supervised process ended, as its record states it."""

    ended: bool
    """whether the record holds an end."""
    returncode: int | None
    """the exit code, or the negative signal number, once ended."""


def read_outcome(record: Path) -> Outcome:
    """
    :param record: the path of a process record.
    :return: what the record states of how the process ended. A record that holds no end, whose process
        and supervisor are both gone, is an end with return code -1: nothing is left to record one.
    """
    fields = json.loads(record.read_text())
    recorded = _recorded_end(fields)
    if recorded is not None:
        return recorded
    if alive(fields.get("pid", 0), fields.get("start", "")) or alive(fields.get("supervisor_pid", 0), fields.get("supervisor_start", "")):
        return Outcome(False, None)

    # the supervisor writes the end before it exits, so a record read before that exit is read again once both are gone
    recorded = _recorded_end(json.loads(record.read_text()))
    return recorded if recorded is not None else Outcome(True, -1)


def _recorded_end(fields: dict[str, Any]) -> Outcome | None:
    """
    :param fields: the fields of a process record.
    :return: the end the record holds, or None when it holds none.
    """
    if not fields.get("ended_at"):
        return None
    code = fields.get("exit_code")
    if code is not None:
        return Outcome(True, code)
    return Outcome(True, -_SIGNALS.get(fields.get("signal", ""), signal.SIGKILL))


def start_time(pid: int) -> str:
    """
    :param pid: a process id.
    :return: the kernel start time of the live process of that id that is not a zombie, in the form brain-supervise
        records it, or "" when there is none. With the pid it names one process, so a pid the kernel has since given
        another process never passes for the one it named.
    """
    if pid <= 0:
        return ""
    if sys.platform.startswith("linux"):
        return _linux_start_time(pid)

    # the kernel's start time, seconds and microseconds, which psutil gives as their sum
    try:
        process = psutil.Process(pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            return ""
        created = process.create_time()
    except psutil.Error:
        return ""
    seconds = int(created)
    microseconds = round((created - seconds) * 1_000_000)
    if microseconds == 1_000_000:
        seconds, microseconds = seconds + 1, 0
    return f"{seconds}.{microseconds:06d}"


def _linux_start_time(pid: int) -> str:
    """
    :param pid: a process id.
    :return: the start time field of the process's stat, as brain-supervise records it, or "" for no live process.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return ""

    # the fields after the command name, which is parenthesised and may hold spaces: the state, and the start time 22nd
    fields = stat[stat.rindex(")") + 1 :].split()
    if fields[0] == "Z":
        return ""
    return fields[22 - 3]


def alive(pid: int, start: str) -> bool:
    """
    :param pid: a process id.
    :param start: the kernel start time of the process it named, as :func:`start_time` gives it.
    :return: whether that process is alive: a live process that is not a zombie has the pid and the start time.
    """
    return start != "" and start_time(pid) == start


@dataclass(frozen=True)
class Waiting:
    """
    What waits on a process's outcome, as serena writes it into the process's record for whichever instance reads the
    record next (decision://brain/daemon_restart_survival/journal/continuation).
    """

    kind: str = ""
    """the name of what consumes the outcome: a kept process's kind, a registered :class:`Continuation`'s kind,
    :data:`INLINE_KIND`, or empty when nothing consumes it."""
    state: Any = None
    """what the consumer needs of the process beyond its record."""
    instance_pid: int = 0
    """the pid of the serena instance that started the process."""
    instance_start: str = ""
    """the kernel start time of that instance."""

    @classmethod
    def of(cls, kind: str, state: Any = None) -> "Waiting":
        """
        :param kind: the name of what consumes the outcome.
        :param state: what the consumer needs of the process beyond its record.
        :return: the description of a process this instance starts.
        """
        return cls(kind, state, os.getpid(), start_time(os.getpid()))

    @classmethod
    def from_fields(cls, fields: Any) -> "Waiting":
        """
        :param fields: the waiting fields of a record, which a record written by another program may lack or hold others in.
        :return: what they describe; nothing waits on a process whose record describes nothing.
        """
        if not isinstance(fields, dict):
            return cls()
        return cls(
            kind=str(fields.get("kind", "")),
            state=fields.get("state"),
            instance_pid=int(fields.get("instance_pid", 0)),
            instance_start=str(fields.get("instance_start", "")),
        )

    def started_by_a_live_instance(self) -> bool:
        """
        :return: whether the instance that started the process is alive, this one included, so the process is that
            instance's to consume.
        """
        return alive(self.instance_pid, self.instance_start)


@dataclass(frozen=True)
class Record:
    """A process record serena holds under ``<root>/serena/process``, as brain-supervise writes it."""

    path: Path
    """where the record is."""
    call_id: str
    """the call waiting on the process's outcome, or empty."""
    waiting: Waiting
    """what waits on the outcome."""
    stdout: str
    """the path of the process's stdout file, or empty when its stdout is no file of the record's."""
    stderr: str
    """the path of its stderr file, or empty likewise."""

    @classmethod
    def load(cls, path: Path) -> "Record":
        """
        :param path: where the record is.
        :return: the record.
        """
        fields = json.loads(path.read_text())
        return cls(
            path=path,
            call_id=fields.get("call_id", ""),
            waiting=Waiting.from_fields(fields.get("waiting")),
            stdout=fields.get("stdout", ""),
            stderr=fields.get("stderr", ""),
        )

    def outcome(self) -> Outcome:
        """
        :return: what the record states of how the process ended; a record that is gone holds an end no one can read.
        """
        try:
            return read_outcome(self.path)
        except FileNotFoundError:
            return Outcome(True, -1)

    def wait(self) -> Outcome:
        """
        :return: the outcome, once the record holds one.
        """
        while not (outcome := self.outcome()).ended:
            time.sleep(WATCH_SECONDS)
        return outcome

    def read(self, stream: str) -> bytes:
        """
        :param stream: :attr:`stdout` or :attr:`stderr`.
        :return: everything the process wrote to it.
        """
        try:
            return Path(stream).read_bytes() if stream else b""
        except FileNotFoundError:
            return b""

    def stop(self) -> Outcome:
        """
        Ends the process by its terminate signal, or by its kill signal when it has not ended within the time a process
        has to end on the first.

        :return: the outcome.
        """
        _signal(self.path, signal.SIGTERM)
        deadline: float | None = time.monotonic() + _STOP_SECONDS
        while not (outcome := self.outcome()).ended:
            if deadline is not None and time.monotonic() >= deadline:
                _signal(self.path, signal.SIGKILL)
                deadline = None
            time.sleep(_END_CHECK_SECONDS)
        return outcome

    def discard(self) -> None:
        """Removes the record and its streams: its outcome has been consumed, and nothing will read them again."""
        _discard(self.path.parent, self.path.stem)


def records() -> list[Record]:
    """
    :return: every record serena holds whose outcome no instance has consumed, oldest first. Serena discards a record
        once it has consumed the outcome, so this is every record but one another server marked consumed.
    """
    found = []
    for path in sorted((restart_root() / OWNER / "process").glob("*.json")):
        try:
            fields = json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            continue
        if not fields.get("consumed"):
            found.append(Record.load(path))
    return found


def reclaim(path: str | os.PathLike, consumed: int) -> None:
    """
    Frees the disk blocks of a stream file that hold only bytes its reader has consumed: every whole block below
    ``consumed``. The file keeps its size and each byte its offset, so an instance taking the process over reads on from
    the offset its state names, and the freed prefix reads as zeros (bug://brain/multiplexer/stdio/stdout/file/growth).
    A file system that frees no blocks so leaves the file as it was.

    :param path: the stream file.
    :param consumed: the first offset the reader has not consumed.
    """
    fd = os.open(path, os.O_WRONLY)
    try:
        length = consumed // os.fstatvfs(fd).f_frsize * os.fstatvfs(fd).f_frsize
        if length == 0:
            return
        if sys.platform == "darwin":
            fcntl.fcntl(fd, _F_PUNCHHOLE, struct.pack("IIqq", 0, 0, 0, length))
        elif sys.platform.startswith("linux"):
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.fallocate(fd, _FALLOC_PUNCH_HOLE, ctypes.c_int64(0), ctypes.c_int64(length)) != 0:
                raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    except OSError as e:
        log.debug("Could not free the consumed prefix of %s: %s", path, e)
    finally:
        os.close(fd)


def _signal(record: Path, sig: int) -> None:
    """
    :param record: a process record.
    :param sig: the signal to send to the process's whole group, once the program has checked the pid and start time.
    """
    name = signal.Signals(sig).name
    subprocess.run([program(), "stop", "-signal", name, str(record)], check=False, stderr=subprocess.DEVNULL)


def _discard(directory: Path, identifier: str) -> None:
    """
    :param directory: the directory of a process record.
    :param identifier: the record's id.
    """
    for suffix in (".json", ".in", ".out", ".err"):
        (directory / f"{identifier}{suffix}").unlink(missing_ok=True)


class _Growth:
    """Waits for a file to grow: by kqueue where there is one, and otherwise by a short sleep."""

    def __init__(self, fd: int) -> None:
        self._queue = None
        if hasattr(select, "kqueue"):
            # registered before the first read, so a write after that read is an event pending at the wait
            self._queue = select.kqueue()
            self._queue.control(
                [
                    select.kevent(
                        fd,
                        filter=select.KQ_FILTER_VNODE,
                        flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                        fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND,
                    )
                ],
                0,
            )

    def wait(self) -> None:
        if self._queue is not None:
            self._queue.control(None, 1, _END_CHECK_SECONDS)
        else:
            time.sleep(0.01)

    def close(self) -> None:
        if self._queue is not None:
            self._queue.close()


class _Follow(io.RawIOBase):
    """
    The reader of a file a process appends to. A read waits for bytes as a read of a pipe does, and
    reads end of file once the process has ended and every byte has been read.
    """

    def __init__(self, path: Path, ended: Callable[[], bool], offset: int = 0) -> None:
        """
        :param path: the file the process appends to.
        :param ended: answers whether the process has ended.
        :param offset: the offset to read from, for an instance taking over a reader that ended.
        """
        super().__init__()
        self._fd = os.open(path, os.O_RDONLY)
        os.lseek(self._fd, offset, os.SEEK_SET)
        self._ended = ended
        self._growth = _Growth(self._fd)

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        while True:
            # the end is read before the bytes: bytes written before the end are read after this check
            ended = self._ended()
            count = os.readv(self._fd, [buffer])
            if count or ended:
                return count
            self._growth.wait()

    def close(self) -> None:
        if not self.closed:
            self._growth.close()
            os.close(self._fd)
        super().close()


class Popen:
    """
    A supervised process, with the interface of ``subprocess.Popen``. Stdin is /dev/null unless it is
    ``PIPE``, a FIFO the process holds open read-write so it never reads end of file, or a file. Stdout
    and stderr given as ``PIPE`` are files the process appends to and :attr:`stdout` and :attr:`stderr`
    follow; stderr may be ``STDOUT``. An inherited stream is this process's own at the start.
    """

    def __init__(
        self,
        args: str | Sequence[str],
        stdin: Any = None,
        stdout: Any = None,
        stderr: Any = None,
        shell: bool = False,
        cwd: str | os.PathLike | None = None,
        env: Mapping[str, str] | None = None,
        text: bool | None = None,
        encoding: str | None = None,
        errors: str | None = None,
        universal_newlines: bool | None = None,
        bufsize: int = -1,
        start_new_session: bool = True,
        creationflags: int = 0,
        call_id: str | None = None,
        waiting: Waiting | None = None,
        keep: bool = False,
        input_bytes: bytes | None = None,
        read_only_stdin: bool = False,
    ) -> None:
        """
        :param args: as for ``subprocess.Popen``.
        :param stdin: as for ``subprocess.Popen``.
        :param stdout: as for ``subprocess.Popen``.
        :param stderr: as for ``subprocess.Popen``.
        :param shell: as for ``subprocess.Popen``.
        :param cwd: as for ``subprocess.Popen``.
        :param env: as for ``subprocess.Popen``.
        :param text: as for ``subprocess.Popen``.
        :param encoding: as for ``subprocess.Popen``.
        :param errors: as for ``subprocess.Popen``.
        :param universal_newlines: as for ``subprocess.Popen``.
        :param bufsize: as for ``subprocess.Popen``: 0 leaves the process's streams unbuffered here.
        :param start_new_session: accepted for ``subprocess.Popen``'s callers; every supervised process
            is already out of serena's session.
        :param creationflags: accepted for ``subprocess.Popen``'s callers, and unused where serena runs.
        :param call_id: the call waiting on the process's outcome; None names the call :data:`CALL_ID` names, and ""
            names none, for a process that outlives the call that started it or that no call waits on.
        :param waiting: what consumes the outcome, for serena's next instance; None is :data:`INLINE_KIND` for a
            process a call waits on, and nothing otherwise.
        :param keep: whether the record and its streams stay once the process's outcome has been read, for a process
            serena's next instance takes over: :meth:`release` removes them.
        :param input_bytes: the bytes the process reads as its standard input, to their end.
        :param read_only_stdin: whether the process's descriptor 0 is its FIFO opened read-only, for a
            program that sizes its reads with FIONREAD: see :data:`_READ_ONLY_STDIN`.
        """
        self.args = args
        self.returncode: int | None = None
        self.stdin: IO[Any] | None = None
        self.stdout: IO[Any] | None = None
        self.stderr: IO[Any] | None = None
        self._text = bool(text or encoding or errors or universal_newlines)
        self._encoding = encoding
        self._errors = errors
        self._bufsize = bufsize
        self._kept = keep
        self._stdout_path: Path | None = None
        self.identifier = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + "-" + token_hex(6)

        # the record's directory and the command as the program takes it
        directory = restart_root() / OWNER / "process"
        directory.mkdir(parents=True, exist_ok=True)
        self.record = directory / f"{self.identifier}.json"
        self._directory = directory
        argv = _argv(args, shell)
        path_var = (env if env is not None else os.environ).get("PATH")
        resolved = shutil.which(argv[0], path=path_var)
        if resolved is None:
            raise _not_runnable(argv[0], path_var)
        if cwd is not None and not os.path.isdir(cwd):
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), os.fspath(cwd))
        if read_only_stdin:
            argv = ["/bin/sh", "-c", _READ_ONLY_STDIN, str(directory / f"{self.identifier}.in"), *argv]
            resolved = argv[0]

        # the call waiting on the process, and what consumes its outcome, which names this instance as the starter
        if call_id is None:
            call_id = CALL_ID.get() or ""
        if waiting is None:
            waiting = Waiting.of(INLINE_KIND if call_id else "")

        spec: dict[str, Any] = {
            "owner": OWNER,
            "id": self.identifier,
            "root": str(restart_root()),
            "call_id": call_id,
            "waiting": asdict(waiting),
            "path": resolved,
            "args": argv,
            "dir": os.fspath(cwd) if cwd is not None else "",
            "env": None if env is None else [f"{key}={value}" for key, value in env.items()],
        }

        # the streams, each a path or a descriptor the supervisor hands on
        handed: list[int] = []
        try:
            self._place_stdin(spec, stdin, input_bytes, handed)
            self._place_output(spec, "stdout", stdout, handed)
            self._place_output(spec, "stderr", stderr, handed)
            report = self._start(spec, handed)
        finally:
            for fd in handed:
                os.close(fd)

        if report.get("error"):
            raise OSError(report["error"])
        self.pid: int = report["pid"]
        self._open_ends(spec, stdin, stdout, stderr)

    @classmethod
    def adopt(cls, record: Path, stdout_offset: int = 0) -> "Popen":
        """
        :param record: the record of a process an ended instance started.
        :param stdout_offset: the first offset of stdout that instance did not consume.
        :return: the process, its FIFO open for writing and its output followed from the offsets given.
        """
        fields = json.loads(record.read_text())
        self = cls.__new__(cls)
        self.args = fields.get("args")
        self.returncode = None
        self._text = False
        self._encoding = None
        self._errors = None
        self._bufsize = -1
        self._kept = True
        self._stdout_path = None
        self.identifier = fields["id"]
        self.record = record
        self._directory = record.parent
        self.pid = fields.get("pid", 0)
        self.stdin = None
        self.stdout = None
        self.stderr = None
        self._attach(fields, stdout_offset)
        return self

    def _place_stdin(self, spec: dict, stdin: Any, input_bytes: bytes | None, handed: list[int]) -> None:
        if input_bytes is not None:
            with tempfile.TemporaryFile() as file:
                file.write(input_bytes)
                file.seek(0)
                spec["stdin_fd"] = self._hand(file.fileno(), handed)
        elif stdin == subprocess.PIPE:
            spec["stdin"] = str(self._directory / f"{self.identifier}.in")
        elif stdin not in (None, subprocess.DEVNULL):
            spec["stdin_fd"] = self._hand(stdin if isinstance(stdin, int) else stdin.fileno(), handed)

    def _place_output(self, spec: dict, name: str, target: Any, handed: list[int]) -> None:
        if target == subprocess.DEVNULL:
            return
        if target == subprocess.STDOUT:
            if "stdout" in spec:
                spec[name] = spec["stdout"]
            elif "stdout_fd" in spec:
                spec["stderr_fd"] = spec["stdout_fd"]
        elif target == subprocess.PIPE:
            spec[name] = str(self._directory / f"{self.identifier}.{name[3:]}")
        else:
            own = {"stdout": 1, "stderr": 2}[name]
            descriptor = own if target is None else _descriptor(target)

            # a stream this process inherited that is a pipe or a socket, which brain-supervise refuses since a started
            # process must outlive the other end: its output goes to the record's file
            if target is None and _is_pipe_or_socket(descriptor):
                spec[name] = str(self._directory / f"{self.identifier}.{name[3:]}")
            else:
                spec[f"{name}_fd"] = self._hand(descriptor, handed)

    @staticmethod
    def _hand(fd: int, handed: list[int]) -> int:
        duplicate = os.dup(fd)
        handed.append(duplicate)
        return duplicate

    @staticmethod
    def _start(spec: dict, handed: list[int]) -> dict:
        # new process group, as the supervisor leads a session of its own once it runs
        supervisor = subprocess.Popen(
            [program(), "run"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            pass_fds=tuple(handed),
        )
        assert supervisor.stdin is not None and supervisor.stdout is not None
        # what to start, which a supervisor that ended at once never reads
        _give_spec(supervisor.stdin, spec)
        line = supervisor.stdout.readline()
        supervisor.stdout.close()

        # the supervisor runs on after its report, and nothing else reaps it while this instance runs
        threading.Thread(target=supervisor.wait, name="supervisor-reaper", daemon=True).start()
        if not line:
            raise OSError("brain-supervise reported nothing")
        return json.loads(line)

    def _open_ends(self, spec: dict, stdin: Any, stdout: Any, stderr: Any) -> None:
        if stdin == subprocess.PIPE:
            self.stdin = self._wrap_writer(self._open_fifo(spec["stdin"]))
        if stdout == subprocess.PIPE:
            self._stdout_path = Path(spec["stdout"])
            self.stdout = self._wrap_reader(self._stdout_path, 0)
        if stderr == subprocess.PIPE:
            self.stderr = self._wrap_reader(Path(spec["stderr"]), 0)

    def _attach(self, fields: dict, stdout_offset: int) -> None:
        if fields.get("stdin"):
            self.stdin = self._wrap_writer(self._open_fifo(fields["stdin"]))
        if fields.get("stdout"):
            self._stdout_path = Path(fields["stdout"])
            self.stdout = self._wrap_reader(self._stdout_path, stdout_offset)
        if fields.get("stderr") and fields.get("stderr") != fields.get("stdout"):
            self.stderr = self._wrap_reader(Path(fields["stderr"]), os.path.getsize(fields["stderr"]))

    def _open_fifo(self, path: str) -> int:
        """
        :param path: the process's FIFO.
        :return: a descriptor writing to it. An open for writing waits for a reader, and a process that
            ended before it opened its FIFO never will be one, so this waits only while the process runs;
            once it has ended the descriptor is read-write, which takes writes that nothing reads.
        """
        while True:
            try:
                fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as e:
                if e.errno != errno.ENXIO:
                    raise
                if self.poll() is not None:
                    return os.open(path, os.O_RDWR)
                time.sleep(0.001)
                continue
            os.set_blocking(fd, True)
            return fd

    def _wrap_writer(self, fd: int) -> IO[Any]:
        writer = os.fdopen(fd, "wb", buffering=0 if self._bufsize == 0 else -1)
        if self._text:
            return io.TextIOWrapper(writer, encoding=self._encoding, errors=self._errors, write_through=True)
        return writer

    def _wrap_reader(self, path: Path, offset: int) -> IO[Any]:
        follow = _Follow(path, lambda: self.poll() is not None, offset)
        if self._bufsize == 0 and not self._text:
            return follow  # type: ignore[return-value]
        reader = io.BufferedReader(follow)
        if self._text:
            return io.TextIOWrapper(reader, encoding=self._encoding, errors=self._errors)
        return reader

    def poll(self) -> int | None:
        """
        :return: the return code once the process has ended, and otherwise None.
        """
        if self.returncode is None:
            try:
                outcome = read_outcome(self.record)
            except FileNotFoundError:
                return self.returncode
            if outcome.ended:
                self.returncode = outcome.returncode
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        """
        :param timeout: how long to wait, in seconds; None waits for as long as the process runs.
        :return: the return code.
        :raises subprocess.TimeoutExpired: when the process runs past the timeout.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        delay = 0.001
        while self.poll() is None:
            if deadline is not None and timeout is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(delay)
            delay = min(delay * 2, _END_CHECK_SECONDS)
        assert self.returncode is not None
        return self.returncode

    def communicate(self, input: bytes | str | None = None, timeout: float | None = None) -> tuple[Any, Any]:
        """
        :param input: what to write to the process's FIFO.
        :param timeout: how long to wait, in seconds.
        :return: the process's stdout and stderr, each None unless it was ``PIPE``.
        :raises subprocess.TimeoutExpired: when the process runs past the timeout.
        """
        if input and self.stdin is not None:
            self.stdin.write(input)  # type: ignore[arg-type]
            self.stdin.flush()
        self.wait(timeout)
        stdout = self.stdout.read() if self.stdout is not None else None
        stderr = self.stderr.read() if self.stderr is not None else None
        self._discard()
        return stdout, stderr

    def send_signal(self, sig: int) -> None:
        """
        :param sig: the signal to send to the process's whole group, one of the program's named signals.
        """
        if self.poll() is None:
            _signal(self.record, sig)

    def terminate(self) -> None:
        """Sends SIGTERM to the process's whole group."""
        self.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        """Sends SIGKILL to the process's whole group."""
        self.send_signal(signal.SIGKILL)

    def _discard(self) -> None:
        """Removes the record and its streams, once nothing will read them; open readers keep their bytes."""
        if self._kept:
            return
        _discard(self._directory, self.identifier)

    def reclaim_stdout(self, consumed: int) -> None:
        """
        :param consumed: the first offset of the process's stdout file that its reader has not consumed, which a
            next instance taking the process over reads on from: the blocks below it are freed, see :func:`reclaim`.
        """
        if self._stdout_path is not None:
            reclaim(self._stdout_path, consumed)

    def release(self) -> None:
        """Removes a kept record and its streams: nothing will read them again."""
        self._kept = False
        self._discard()

    def close_streams(self) -> None:
        """Closes this instance's ends of the process's streams."""
        for stream in (self.stdin, self.stdout, self.stderr):
            if stream is not None:
                stream.close()

    def __enter__(self) -> "Popen":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close_streams()


def _not_runnable(name: str, path_var: str | None) -> OSError:
    """
    :param name: the program a start could not resolve.
    :param path_var: the PATH it searched.
    :return: what exec answers: a :class:`PermissionError` where something of that name exists, as a file without its
        execute bit or a directory does, and a :class:`FileNotFoundError` where nothing does.
    """
    directories = [""] if os.path.dirname(name) else (path_var if path_var is not None else os.defpath).split(os.pathsep)
    code = errno.EACCES if any(os.path.exists(os.path.join(directory, name)) for directory in directories) else errno.ENOENT
    return OSError(code, os.strerror(code), name)


def _argv(args: str | Sequence[str], shell: bool) -> list[str]:
    command = [args] if isinstance(args, str | os.PathLike) else [os.fspath(arg) for arg in args]
    if shell:
        return ["/bin/sh", "-c", *command]
    return [os.fspath(command[0]), *command[1:]]


def _descriptor(target: Any) -> int:
    return target if isinstance(target, int) else target.fileno()


def _is_pipe_or_socket(fd: int) -> bool:
    """
    :param fd: an open descriptor.
    :return: whether it is a pipe or a socket.
    """
    mode = os.fstat(fd).st_mode
    return stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)


def run(
    args: str | Sequence[str],
    *,
    input: bytes | str | None = None,
    capture_output: bool = False,
    timeout: float | None = None,
    check: bool = False,
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    """
    :param args: as for ``subprocess.run``.
    :param input: as for ``subprocess.run``.
    :param capture_output: as for ``subprocess.run``.
    :param timeout: as for ``subprocess.run``.
    :param check: as for ``subprocess.run``.
    :param kwargs: as for :class:`Popen`.
    :return: the completed process.
    :raises subprocess.TimeoutExpired: when the process runs past the timeout; it is killed first.
    :raises subprocess.CalledProcessError: when check is set and the process failed.
    """
    if capture_output:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    if input is not None:
        encoding = kwargs.get("encoding") or "utf-8"
        kwargs["input_bytes"] = input.encode(encoding) if isinstance(input, str) else input

    with Popen(args, **kwargs) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # killed, and its record and streams removed as communicate removes those of a process that ended
            process.kill()
            process.wait()
            process._discard()
            raise
    code = process.returncode
    assert code is not None
    if check and code:
        raise subprocess.CalledProcessError(code, args, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(args, code, stdout, stderr)


def check_output(args: str | Sequence[str], *, timeout: float | None = None, **kwargs: Any) -> Any:
    """
    :param args: as for ``subprocess.check_output``.
    :param timeout: as for ``subprocess.check_output``.
    :param kwargs: as for :class:`Popen`.
    :return: the process's stdout.
    :raises subprocess.CalledProcessError: when the process failed.
    """
    kwargs["stdout"] = subprocess.PIPE
    return run(args, timeout=timeout, check=True, **kwargs).stdout


class Continuation(ABC):
    """
    A tool's own code for the result of the call that waited on a process, computed from what the process's record
    holds for it and from its ended record. The instance that started the process computes the call's result through it
    too, so a restart cannot change the answer (decision://brain/daemon_restart_survival/journal/continuation).
    """

    @abstractmethod
    def result(self, state: Any, record: Record) -> Any:
        """
        :param state: the state the record holds for the continuation.
        :param record: the record of the process, which has ended.
        :return: the call's result, in the form serena answers calls with.
        """

    @abstractmethod
    def failure(self, error: Exception) -> Any:
        """
        :param error: what :meth:`result` raised.
        :return: the call's result when its result could not be computed, in the same form.
        """


class Continuations:
    """The registry of :class:`Continuation` objects by the waiting kind that names each."""

    def __init__(self) -> None:
        self._by_kind: dict[str, Continuation] = {}

    def register(self, kind: str, continuation: Continuation) -> None:
        """
        :param kind: the waiting kind of the processes whose calls the continuation answers.
        :param continuation: the continuation.
        """
        self._by_kind[kind] = continuation

    def get(self, kind: str) -> Continuation | None:
        """
        :param kind: a waiting kind.
        :return: the continuation registered for it, or None.
        """
        return self._by_kind.get(kind)


CONTINUATIONS = Continuations()
"""The continuations of serena's tools, each registered by the module that starts its processes."""


class Calls(Protocol):
    """The journal of the calls serena serves, as startup adoption answers them: serena's ``CallJournal``."""

    def claim(self, call_id: str) -> None:
        """
        :param call_id: a call an ended instance received, which this instance now answers: a resume of it waits for
            :meth:`finish`.
        """

    def finish(self, call_id: str, result: Any) -> None:
        """
        :param call_id: the call.
        :param result: its result.
        """

    def resume(self, call_id: str) -> Any:
        """
        :param call_id: the call.
        :return: its result; for a call no live instance answers and none answered, the failure naming the restart,
            journaled as its result.
        """


class Adoption:
    """
    Serena's adoption, when it starts, of every record an ended instance left whose outcome no instance has consumed
    (pg://brain/daemon/restart/readoption), but those of kept processes, whose own states take them over. A record naming
    a registered continuation has the continuation's result written to its call's entry once its process has ended; a
    record naming none is waited on and discarded, and its call, if it has one, is answered with the restart failure
    (decision://brain/daemon_restart_survival/journal/continuation). A kept process no state names can never be taken
    over, so it is stopped. Every record is discarded once its outcome has been consumed.
    """

    def __init__(
        self, calls: Calls, kept_kinds: Collection[str], kept_records: Collection[Path], continuations: Continuations = CONTINUATIONS
    ) -> None:
        """
        :param calls: the journal of the calls serena serves.
        :param kept_kinds: the waiting kinds of kept processes.
        :param kept_records: the records some state of a kept process names.
        :param continuations: the registry of continuations.
        """
        self._calls = calls
        self._kept_kinds = set(kept_kinds)
        self._kept_records = {Path(record) for record in kept_records}
        self._continuations = continuations

    def adopt(self, skip: Collection[Path] = ()) -> list[threading.Thread]:
        """
        Takes up every such record. The call of each process still running is claimed before this returns, so this runs
        before serena serves a call.

        :param skip: the records of processes this instance holds already.
        :return: the threads waiting on the processes still running, each ending once it has consumed its process's
            outcome.
        """
        watchers = []
        held = {Path(record) for record in skip}
        for record in records():
            if record.path in held or record.path in self._kept_records or record.waiting.started_by_a_live_instance():
                continue
            try:
                ended = self._take_up(record)
            except Exception as e:
                log.exception("Could not take up the process of %s: %s", record.path, e)
                continue
            if ended:
                self._consume(record)
            else:
                watcher = threading.Thread(target=self._consume, args=(record,), name=f"adopted-{record.path.stem}", daemon=True)
                watcher.start()
                watchers.append(watcher)
        return watchers

    def _take_up(self, record: Record) -> bool:
        """
        :param record: a record an ended instance left.
        :return: whether its process has ended.
        """
        # a kept process no state names: nothing can take it over
        if record.waiting.kind in self._kept_kinds:
            return record.stop().ended

        # the call, claimed when a continuation answers it, and otherwise answered with the restart failure unless it holds a result
        if self._continuation(record) is not None:
            self._calls.claim(record.call_id)
        elif record.call_id:
            self._calls.resume(record.call_id)
        return record.outcome().ended

    def _consume(self, record: Record) -> None:
        """
        :param record: a record taken up, whose process is waited on, whose call the continuation answers, and which is
            then discarded.
        """
        record.wait()
        continuation = self._continuation(record)
        if continuation is not None:
            try:
                result = continuation.result(record.waiting.state, record)
            except Exception as e:
                log.exception("The continuation of %s failed: %s", record.path, e)
                result = continuation.failure(e)
            try:
                self._calls.finish(record.call_id, result)
            except Exception as e:
                log.exception("Could not answer the call %s of %s: %s", record.call_id, record.path, e)
        record.discard()

    def _continuation(self, record: Record) -> Continuation | None:
        return self._continuations.get(record.waiting.kind) if record.call_id else None


def _give_spec(stdin: Any, spec: dict) -> None:
    """
    Hands the supervisor what to start. A supervisor that ended before reading it reports nothing, which
    :meth:`Popen._start` raises as such, so the broken pipe is no error of its own.

    :param stdin: the supervisor's stdin, closed here.
    :param spec: what to start.
    """
    try:
        stdin.write(json.dumps(spec).encode())
        stdin.close()
    except BrokenPipeError:
        pass
