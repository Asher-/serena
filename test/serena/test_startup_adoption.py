"""
Serena's adoption, when it starts, of the records an ended instance left (pg://brain/daemon/restart/readoption,
decision://brain/daemon_restart_survival/journal/continuation): a shell command's call is answered with the command's
real outcome, a call whose process names no continuation is answered with the restart failure and its record is
discarded once the process ends, a kept process no state names is stopped, and a process a live instance started is
left to it. Each first instance here is a process of its own, killed with its whole group as serena is.
"""

import contextvars
import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from secrets import token_hex

import pytest

from serena.call_journal import CallJournal, CallResult
from serena.util.shell import SHELL_COMMAND_KIND, execute_shell_command
from solidlsp.ls_survival import LANGUAGE_SERVER_KIND
from solidlsp.util import supervise
from test.conftest import SCRATCH_ROOT

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")

SCRATCH = SCRATCH_ROOT
"""The directory under which each test here keeps its files, never a system temp directory."""

_DEADLINE_SECONDS = 60.0

FIRST_INSTANCE = textwrap.dedent(
    """
    import sys, time
    from serena.call_journal import CALL_ID_VAR, CallJournal
    from serena.util.shell import execute_shell_command
    from solidlsp.util import supervise

    journal = CallJournal()
    call_id, how, command = sys.argv[1], sys.argv[2], sys.argv[3]
    journal.receive(call_id, "a_tool", {"command": command})
    CALL_ID_VAR.set(call_id)
    print("started", flush=True)
    if how == "shell":
        execute_shell_command(command, capture_stderr=True)
    else:
        supervise.run(["/bin/sh", "-c", command], capture_output=True)
    time.sleep(300)
    """
)


@pytest.fixture
def scratch(request: pytest.FixtureRequest) -> Iterator[Path]:
    directory = SCRATCH / f"{request.node.name[:48]}-{token_hex(4)}"
    directory.mkdir(parents=True)
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def root(scratch: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The instances' root, whose processes are killed after the test whatever it left running."""
    directory = scratch / "restart"
    monkeypatch.setenv(supervise.ROOT_ENV, str(directory))
    yield directory
    for path in (directory / supervise.OWNER / "process").glob("*.json"):
        if "ended_at" not in json.loads(path.read_text()):
            subprocess.run([supervise.program(), "stop", "-signal", "SIGKILL", str(path)], check=False, stderr=subprocess.DEVNULL)


def _await(condition: object, what: str) -> None:
    deadline = time.monotonic() + _DEADLINE_SECONDS
    while not condition():  # type: ignore[operator]
        assert time.monotonic() < deadline, what
        time.sleep(0.01)


def _records(root: Path) -> list[supervise.Record]:
    return [supervise.Record.load(path) for path in sorted((root / supervise.OWNER / "process").glob("*.json"))]


def _first_instance_killed_while(root: Path, call_id: str, how: str, command: str) -> None:
    """Runs a first instance that serves the call by starting the command, and kills its group once the command runs."""
    instance = subprocess.Popen(
        [sys.executable, "-c", FIRST_INSTANCE, call_id, how, command],
        env={**os.environ, supervise.ROOT_ENV: str(root)},
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert instance.stdout is not None and instance.stdout.readline() == "started\n"
    _await(lambda: any(record.call_id == call_id and record.waiting.kind for record in _records(root)), "the command did not start")
    os.killpg(instance.pid, signal.SIGKILL)
    instance.wait()


def _adoption(journal: CallJournal) -> supervise.Adoption:
    return supervise.Adoption(journal, kept_kinds=(LANGUAGE_SERVER_KIND,), kept_records=())


def test_a_shell_command_running_at_the_kill_has_its_real_outcome_delivered_to_resume(root: Path, scratch: Path) -> None:
    marker = scratch / "go"
    _first_instance_killed_while(root, "shell-1", "shell", f"while [ ! -e {marker} ]; do sleep 0.01; done; echo out; echo err >&2; exit 3")

    journal = CallJournal()
    watchers = _adoption(journal).adopt()
    resumed: list[CallResult] = []
    resumer = threading.Thread(target=lambda: resumed.append(journal.resume("shell-1")))
    resumer.start()
    marker.touch()
    resumer.join(_DEADLINE_SECONDS)
    for watcher in watchers:
        watcher.join(_DEADLINE_SECONDS)

    assert resumed and resumed[0].is_error is False
    assert json.loads(resumed[0].text) == {"stdout": "out\n", "stderr": "err\n", "return_code": 3, "cwd": os.getcwd()}
    assert _records(root) == []


def test_a_shell_command_that_ended_while_no_instance_ran_has_its_outcome_delivered_at_startup(root: Path, scratch: Path) -> None:
    marker = scratch / "go"
    _first_instance_killed_while(root, "shell-2", "shell", f"while [ ! -e {marker} ]; do sleep 0.01; done; echo done")
    marker.touch()
    _await(lambda: all(record.outcome().ended for record in _records(root)), "the command did not end")

    journal = CallJournal()
    watchers = _adoption(journal).adopt()

    assert watchers == []
    assert json.loads(journal.resume("shell-2").text)["stdout"] == "done\n"
    assert _records(root) == []


def test_a_call_whose_process_names_no_continuation_is_answered_with_the_restart_and_its_record_discarded(
    root: Path, scratch: Path
) -> None:
    marker = scratch / "go"
    _first_instance_killed_while(root, "inline-1", "inline", f"while [ ! -e {marker} ]; do sleep 0.01; done")

    journal = CallJournal()
    watchers = _adoption(journal).adopt()
    answer = journal.resume("inline-1")
    assert [record.outcome().ended for record in _records(root)] == [False]
    marker.touch()
    for watcher in watchers:
        watcher.join(_DEADLINE_SECONDS)

    assert answer.is_error and "restarted" in answer.text
    assert _records(root) == []


def test_a_process_a_live_instance_started_is_left_to_it(root: Path) -> None:
    process = supervise.Popen(["/bin/sleep", "300"], keep=True)

    watchers = _adoption(CallJournal()).adopt()

    assert watchers == []
    assert process.poll() is None
    assert [record.path for record in _records(root)] == [process.record]
    process.kill()
    process.wait()
    process.release()


def test_a_kept_process_no_state_names_is_stopped_and_its_record_discarded(root: Path) -> None:
    starter = textwrap.dedent(
        """
        import sys, time
        from solidlsp.util import supervise
        supervise.Popen(["/bin/sleep", "300"], call_id="", waiting=supervise.Waiting.of(sys.argv[1]), keep=True)
        print("started", flush=True)
        time.sleep(300)
        """
    )
    instance = subprocess.Popen(
        [sys.executable, "-c", starter, LANGUAGE_SERVER_KIND],
        env={**os.environ, supervise.ROOT_ENV: str(root)},
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert instance.stdout is not None and instance.stdout.readline() == "started\n"
    os.killpg(instance.pid, signal.SIGKILL)
    instance.wait()
    pid = json.loads(_records(root)[0].path.read_text())["pid"]

    _adoption(CallJournal()).adopt()

    assert _records(root) == []
    assert not supervise.start_time(pid)


def test_a_shell_command_names_the_call_it_serves_and_its_continuation(root: Path, scratch: Path) -> None:
    marker = scratch / "go"
    results = []

    # the command run in a context serving a call, as serena's task executor runs a tool in the call's context
    def serve() -> None:
        supervise.CALL_ID.set("shell-3")
        results.append(execute_shell_command(f"while [ ! -e {marker} ]; do sleep 0.01; done; echo out", cwd=str(scratch)))

    command = threading.Thread(target=contextvars.copy_context().run, args=(serve,))
    command.start()
    _await(lambda: _records(root), "the command did not start")
    record = _records(root)[0]
    marker.touch()
    command.join(_DEADLINE_SECONDS)

    assert (record.call_id, record.waiting.kind, record.waiting.state) == (
        "shell-3",
        SHELL_COMMAND_KIND,
        {"cwd": str(scratch), "capture_stderr": False},
    )
    assert [(result.stdout, result.return_code, result.stderr) for result in results] == [("out\n", 0, None)]
    assert _records(root) == []


def test_a_shell_command_given_no_directory_runs_in_the_current_one(root: Path) -> None:
    result = execute_shell_command("pwd")

    assert result.cwd == str(Path.cwd())
    assert (Path(result.stdout.strip()).resolve(), result.return_code) == (Path.cwd().resolve(), 0)
    assert _records(root) == []
