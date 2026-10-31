"""
Every process serena starts, started through the brain-supervise program.

A supervised process leads a process group of its own under a supervisor that leads a session of
its own, writes its streams to files and FIFOs rather than to pipes serena holds, and has its outcome
recorded whether or not serena is running, so it outlives a restart of serena
(pg://brain/daemon/restart/survival). :class:`Popen` and :func:`run` stand where ``subprocess.Popen``
and ``subprocess.run`` stood; ``subprocess.PIPE``, ``subprocess.DEVNULL``, ``subprocess.STDOUT`` and its
exceptions keep their meanings. This module is the one place serena calls ``subprocess`` to start
anything, and what it starts is the supervisor.
"""

import errno
import io
import json
import os
import select
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from secrets import token_hex
from typing import IO, Any

OWNER = "serena"
"""The server whose records these are: they live under ``<root>/serena/process``."""

PROGRAM_ENV = "BRAIN_SUPERVISE"
"""The environment variable naming the brain-supervise program; PATH is searched without it."""

ROOT_ENV = "BRAIN_RESTART_ROOT"
"""The environment variable naming the directory that holds every server's records."""

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
    if fields.get("ended_at"):
        code = fields.get("exit_code")
        if code is not None:
            return Outcome(True, code)
        return Outcome(True, -_SIGNALS.get(fields.get("signal", ""), signal.SIGKILL))
    if process_exists(fields.get("pid", 0)) or process_exists(fields.get("supervisor_pid", 0)):
        return Outcome(False, None)
    return Outcome(True, -1)


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
        call_id: str = "",
        waiting: Any = None,
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
        :param call_id: the call waiting on the process's outcome, if any.
        :param waiting: the description of what consumes the outcome, for serena's next instance.
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
        self._kept = waiting is not None
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
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), argv[0])
        if cwd is not None and not os.path.isdir(cwd):
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), os.fspath(cwd))
        if read_only_stdin:
            argv = ["/bin/sh", "-c", _READ_ONLY_STDIN, str(directory / f"{self.identifier}.in"), *argv]
            resolved = argv[0]

        spec: dict[str, Any] = {
            "owner": OWNER,
            "id": self.identifier,
            "root": str(restart_root()),
            "call_id": call_id,
            "path": resolved,
            "args": argv,
            "dir": os.fspath(cwd) if cwd is not None else "",
            "env": None if env is None else [f"{key}={value}" for key, value in env.items()],
        }
        if waiting is not None:
            spec["waiting"] = waiting

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
            spec[f"{name}_fd"] = self._hand(own if target is None else _descriptor(target), handed)

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
        supervisor.stdin.write(json.dumps(spec).encode())
        supervisor.stdin.close()
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
            self.stdout = self._wrap_reader(Path(spec["stdout"]), 0)
        if stderr == subprocess.PIPE:
            self.stderr = self._wrap_reader(Path(spec["stderr"]), 0)

    def _attach(self, fields: dict, stdout_offset: int) -> None:
        if fields.get("stdin"):
            self.stdin = self._wrap_writer(self._open_fifo(fields["stdin"]))
        if fields.get("stdout"):
            self.stdout = self._wrap_reader(Path(fields["stdout"]), stdout_offset)
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
            name = signal.Signals(sig).name
            subprocess.run([program(), "stop", "-signal", name, str(self.record)], check=False, stderr=subprocess.DEVNULL)

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
        for suffix in (".json", ".in", ".out", ".err"):
            (self._directory / f"{self.identifier}{suffix}").unlink(missing_ok=True)

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


def _argv(args: str | Sequence[str], shell: bool) -> list[str]:
    command = [args] if isinstance(args, str | os.PathLike) else [os.fspath(arg) for arg in args]
    if shell:
        return ["/bin/sh", "-c", *command]
    return [os.fspath(command[0]), *command[1:]]


def _descriptor(target: Any) -> int:
    return target if isinstance(target, int) else target.fileno()


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
            process.kill()
            process.wait()
            raise
    code = process.returncode
    assert code is not None
    if check and code:
        raise subprocess.CalledProcessError(code, args, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(args, code, stdout, stderr)


def call(args: str | Sequence[str], *, timeout: float | None = None, **kwargs: Any) -> int:
    """
    :param args: as for ``subprocess.call``.
    :param timeout: as for ``subprocess.call``.
    :param kwargs: as for :class:`Popen`.
    :return: the process's return code.
    """
    return run(args, timeout=timeout, **kwargs).returncode


def check_call(args: str | Sequence[str], *, timeout: float | None = None, **kwargs: Any) -> int:
    """
    :param args: as for ``subprocess.check_call``.
    :param timeout: as for ``subprocess.check_call``.
    :param kwargs: as for :class:`Popen`.
    :return: 0.
    :raises subprocess.CalledProcessError: when the process failed.
    """
    run(args, timeout=timeout, check=True, **kwargs)
    return 0


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
