import os
import subprocess
from dataclasses import asdict, dataclass
from typing import Any

from pydantic import BaseModel

from serena.call_journal import CALL_ID_VAR, CallResult
from solidlsp.util import supervise
from solidlsp.util.subprocess_util import subprocess_kwargs

SHELL_COMMAND_KIND = "serena.shell_command"
"""The waiting kind of a shell command's record, which names :class:`ShellCommandContinuation`."""


class ShellCommandResult(BaseModel):
    stdout: str
    return_code: int
    cwd: str
    stderr: str | None = None


@dataclass(frozen=True)
class ShellCommand:
    """What a shell command's record holds for computing the command's result, beyond the record itself."""

    cwd: str
    """the working directory the command ran in."""
    capture_stderr: bool
    """whether the command's stderr is part of its result."""

    def result(self, record: supervise.Record) -> ShellCommandResult:
        """
        :param record: the record of the command's process, which has ended.
        :return: the command's result, the same whichever instance of serena computes it.
        """
        stderr = record.read(record.stderr).decode("utf-8", errors="replace") if self.capture_stderr else None
        return_code = record.outcome().returncode
        assert return_code is not None
        return ShellCommandResult(
            stdout=record.read(record.stdout).decode("utf-8", errors="replace"), stderr=stderr, return_code=return_code, cwd=self.cwd
        )


class ShellCommandContinuation(supervise.Continuation):
    """
    The result of a call that waited on a shell command, for the instance of serena after the one that started the
    command: the command's :class:`ShellCommandResult` as JSON, computed from its record.
    """

    def result(self, state: Any, record: supervise.Record) -> CallResult:
        return CallResult(ShellCommand(**state).result(record).model_dump_json())

    def failure(self, error: Exception) -> CallResult:
        return CallResult(f"Error: the result of the shell command could not be read from its record: {error}", True)


supervise.CONTINUATIONS.register(SHELL_COMMAND_KIND, ShellCommandContinuation())


def execute_shell_command(command: str, cwd: str | None = None, capture_stderr: bool = False) -> ShellCommandResult:
    """
    Execute a shell command and return the output. The command's record names the call being served, if any, and the
    continuation that computes the call's result from the record, so the instance of serena after a restart delivers
    the command's outcome (decision://brain/daemon_restart_survival/journal/continuation).

    :param command: The command to execute.
    :param cwd: The working directory to execute the command in. If None, the current working directory will be used.
    :param capture_stderr: Whether to capture the stderr output.
    :return: The output of the command.
    """
    if cwd is None:
        cwd = os.getcwd()

    # the command, under the call it serves, whose result is computed from its record as a next instance computes it
    shell_command = ShellCommand(cwd=cwd, capture_stderr=capture_stderr)
    with supervise.Popen(
        command,
        shell=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if capture_stderr else None,
        cwd=cwd,
        call_id=CALL_ID_VAR.get() or "",
        waiting=supervise.Waiting.of(SHELL_COMMAND_KIND, asdict(shell_command)),
        **subprocess_kwargs(),
    ) as process:
        process.wait()
        result = shell_command.result(supervise.Record.load(process.record))
    process.release()
    return result


def subprocess_check_output(
    args: list[str], encoding: str = "utf-8", strip: bool = True, timeout: float | None = None, cwd: str | None = None
) -> str:
    output = supervise.check_output(
        args, stdin=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout, env=os.environ.copy(), cwd=cwd, **subprocess_kwargs()
    ).decode(encoding)  # type: ignore
    if strip:
        output = output.strip()
    return output
