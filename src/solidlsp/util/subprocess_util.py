# SPDX-License-Identifier: MIT

import logging
import os
import platform
import signal
import subprocess
from time import monotonic
from typing import IO, Generic, TypeVar

import oslex
import psutil
from sensai.util.string import ToStringMixin

from solidlsp.util import supervise

log = logging.getLogger(__name__)
TStream = TypeVar("TStream", bound=str | bytes)


def subprocess_kwargs() -> dict:
    """
    Returns a dictionary of keyword arguments for subprocess calls, adding platform-specific
    flags that we want to use consistently.
    """
    kwargs = {}
    if platform.system() == "Windows":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


def subprocess_run(
    cmd: list[str] | str, timeout: int | None = None, check: bool = False, capture_output: bool = True, text: bool = True, **kwargs
) -> subprocess.CompletedProcess:
    """
    Runs a command in a subprocess, applying safe default settings.

    The stdin of the subprocess is set to DEVNULL to avoid interference with the parent process' stdin;
    this cannot be overridden by passing a different value for stdin in kwargs.

    :param cmd: the command to run, specified as a list of arguments or a string
    :param timeout: the timeout in seconds for the command to complete; if None, no timeout is applied
    :param check: if True, raises CalledProcessError if the command exits with a non-zero status
    :param capture_output: if True, captures stdout and stderr; otherwise, they are not captured
    :param text: if True, captures output as text (str); otherwise, captures as bytes
    :return: a CompletedProcess instance containing information about the completed process
    """
    kwargs = dict(kwargs)
    kwargs.update(subprocess_kwargs())
    kwargs.update(
        {
            "timeout": timeout,
            "capture_output": capture_output,
            "text": text,
            "stdin": subprocess.DEVNULL,  # important to avoid interference with parent process' stdin
        }
    )
    return supervise.run(cmd, check=check, **kwargs)


def convert_shell_cmd(cmd: str | list[str]) -> str:
    """
    Converts a command (specified as a list or string) to a format supported by subprocess calls with shell=True on the current platform,
    applying necessary escaping and quoting if the command is specified as a list of arguments.

    :param cmd: the command to convert, specified as a list of arguments
    :return: a suitable representation of the command for subprocess calls on the current platform
    """
    return oslex.join(cmd) if isinstance(cmd, list) else cmd


class ManagedSubprocess(Generic[TStream], ToStringMixin):
    """
    Represents a subprocess.Popen instance with additional lifecycle management, including the ability to terminate the process
    and its children gracefully, with a fallback to forceful termination if necessary.
    """

    def __init__(self, popen: "subprocess.Popen[TStream]", name: str, start_new_session: bool) -> None:
        """
        :param popen: the subprocess.Popen instance representing the launched process
        :param name: the name of the process (used for logging purposes); should start with a capital letter
        :param start_new_session: whether the process was launched in its own session, i.e. whether it is the
            leader of its own process group
        """
        self._popen = popen
        self._name = name

        # a process launched with start_new_session=True is its own group leader, so its PGID is its PID
        self._process_group_id = popen.pid if (start_new_session and os.name == "posix") else None

    def _tostring_includes(self) -> list[str]:
        return ["_name"]

    @property
    def stdin(self) -> "IO[TStream] | None":
        return self._popen.stdin

    @property
    def stdout(self) -> "IO[TStream] | None":
        return self._popen.stdout

    @property
    def stderr(self) -> "IO[TStream] | None":
        return self._popen.stderr

    @property
    def returncode(self) -> int | None:
        """
        :return: the exit code of the process as of the most recent status check (see :meth:`poll`),
            or None if the process was not yet known to have terminated
        """
        return self._popen.returncode

    def poll(self) -> int | None:
        """
        Checks whether the process has terminated, updating :attr:`returncode` accordingly.

        :return: the exit code of the process or None if it is still running
        """
        return self._popen.poll()

    def terminate(self, timeout: float) -> None:
        """
        Terminates the process and its children, forcefully killing them if they do not exit in time.

        :param timeout: the time, in seconds, to wait for the process to terminate gracefully before killing it
        """
        terminate_process_tree_with_kill_fallback(
            self._popen,
            terminate_timeout=timeout,
            process_name=self._name,
            process_group_id=self._process_group_id,
        )


def _signal_process_tree(
    process: "subprocess.Popen[bytes]",
    terminate: bool = True,
    descendants: list[psutil.Process] | None = None,
) -> None:
    """
    Sends a signal (terminate or kill) to the given process and its descendants.

    ``descendants`` is an optional snapshot captured before the leader is signaled.
    It is needed for kill fallback: the leader may have exited and been reaped before
    the fallback runs, in which case re-enumerating from ``process.pid`` loses the tree.

    :param terminate: if True, signal terminate, otherwise signal kill
    :param descendants: previously discovered descendants to signal even if the leader is gone
    """

    def signal_process(p: "subprocess.Popen | psutil.Process") -> None:
        try:
            if terminate:
                p.terminate()
            else:
                p.kill()
        except (psutil.Error, OSError):
            pass

    if descendants is None:
        descendants = []
        try:
            parent = psutil.Process(process.pid)
            if parent.is_running():
                descendants = parent.children(recursive=True)
        except (psutil.Error, OSError):
            pass

    # Signal the snapshot first, then the leader. The snapshot remains usable after
    # the leader has exited, while newly spawned children are outside its guarantee.
    for child in descendants:
        signal_process(child)
    signal_process(process)


def _signal_process_group(pgid: int, terminate: bool = True) -> None:
    """
    Sends a signal to every process in the given POSIX process group by group ID, without
    enumerating the system process table. Requires the caller to know the group already exists
    and is owned by us (see ``terminate_process_tree_with_kill_fallback``'s ``process_group_id``).

    :param pgid: the process group ID to signal
    :param terminate: if True, signal terminate (SIGTERM), otherwise signal kill (SIGKILL)
    """
    sig = signal.SIGTERM if terminate else signal.SIGKILL
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        # the group is already gone; nothing left to signal
        pass
    except PermissionError as e:
        log.warning(f"Permission denied signaling process group {pgid} with {sig.name}: {e}")
    except Exception as e:
        log.warning(f"Unexpected error signaling process group {pgid} with {sig.name}: {e}")


def _get_process_descendants(process: "subprocess.Popen") -> list[psutil.Process]:
    """Return a snapshot of descendants that should be waited on after signaling."""
    try:
        return psutil.Process(process.pid).children(recursive=True)
    except (psutil.Error, OSError):
        return []


def _wait_for_processes_until(processes: list[psutil.Process], deadline: float) -> bool:
    """Wait for a process snapshot until an absolute monotonic deadline.

    Descendants are waited in reverse depth-first discovery order so that a child
    has a chance to exit before its parent is reaped. A process that disappears
    or cannot be waited by the current OS process is already clean for our
    purposes; a timeout is returned to trigger the caller's kill fallback.
    """
    for child in reversed(processes):
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        try:
            child.wait(timeout=remaining)
        except psutil.TimeoutExpired:
            return False
        except (psutil.Error, OSError):
            continue
    return True


def _wait_for_processes(processes: list[psutil.Process], timeout: float) -> bool:
    """Wait for a process snapshot within one shared timeout budget."""
    return _wait_for_processes_until(processes, monotonic() + timeout)


def terminate_process_tree_with_kill_fallback(
    process: "subprocess.Popen",
    terminate_timeout: float,
    process_name: str = "Process",
    process_group_id: int | None = None,
) -> None:
    """
    Attempts to terminate the given process and its children by signaling them to terminate,
    and if that fails (i.e. they don't exit within the given timeout), forcefully kills them.

    The termination is logged.

    :param process: the process to terminate
    :param terminate_timeout: the time to wait for the process to terminate gracefully before killing it
    :param process_name: the name of the process (used for logging purposes); should start with capital letter
    :param process_group_id: if given, the POSIX process group ID that ``process`` leads (i.e. it was
        launched with ``start_new_session=True``, so its PGID equals its PID). When set, cleanup
        signals the whole group directly via ``os.killpg`` rather than walking the process tree to
        signal it. Descendant discovery remains best-effort so cleanup can wait for children after
        the group is signaled; ``_get_process_descendants`` safely returns an empty list when the
        platform denies process-table enumeration (for example, ``sysctl(KERN_PROC_ALL)`` on macOS).
        Only pass this for a process that was started in its own session: signaling the group of a
        process that shares ours would also signal us.
    """
    log.debug(f"Terminating process {process.pid}, current status: {process.poll()}")
    descendants = _get_process_descendants(process)

    def signal_tree(terminate: bool) -> None:
        if process_group_id is not None:
            _signal_process_group(process_group_id, terminate=terminate)
        else:
            _signal_process_tree(process, terminate=terminate, descendants=None if terminate else descendants)

    signal_tree(terminate=True)
    try:
        log.debug(f"Waiting for process {process.pid} to terminate...")
        terminate_deadline = monotonic() + terminate_timeout
        descendants_finished = _wait_for_processes_until(descendants, terminate_deadline)
        remaining = terminate_deadline - monotonic()
        if not descendants_finished or remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, terminate_timeout)
        exit_code = process.wait(timeout=remaining)
        log.info(f"{process_name} terminated successfully with exit code {exit_code}.")
    except subprocess.TimeoutExpired:
        # If termination failed, forcefully kill the process
        log.warning(f"{process_name} (pid={process.pid}) termination timed out, killing process forcefully...")
        signal_tree(terminate=False)
        kill_deadline = monotonic() + 2.0
        _wait_for_processes_until(descendants, kill_deadline)
        remaining = max(kill_deadline - monotonic(), 0.1)
        try:
            exit_code = process.wait(timeout=remaining)
            log.info(f"{process_name} killed successfully with exit code {exit_code}.")
        except subprocess.TimeoutExpired:
            log.error(f"{process_name} (pid={process.pid}) could not be killed within timeout.")
    except Exception as e:
        log.error(f"Error during process shutdown: {e}")
