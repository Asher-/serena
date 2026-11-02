"""
Stand-in programs, for showing that a function starts the program it runs through brain-supervise.

A stand-in is an executable named as the program a function runs, on a PATH that holds the test's stand-ins and nothing
else. Before it does anything else, each run of a stand-in logs its pid, its parent's and its grandparent's, and copies
beside that log the brain-supervise record whose supervisor is its parent or, for a run a shell started, its
grandparent. A supervisor writes its record, its own pid in it, before it starts its process, and that process is the
run or the shell that started it, so the record is there when the run looks. :func:`solidlsp.util.supervise.run`
removes a finished run's record, so the record is observed by the run itself while the run is under way. A run started
other than through brain-supervise finds no such record.

The body of a stand-in is shell, with the program's arguments in ``$@``. Since PATH holds nothing but stand-ins, a body
names every other program by its absolute path.

Two routes are not reached by a stand-in that runs. A program that is found on PATH but cannot be started is
:meth:`StandIns.unrunnable`. A run that outlasts a long fixed timeout is replaced, for the handler of that timeout, by
:func:`time_out_runs`.
"""

import json
import shlex
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from solidlsp.util import supervise

_SCRIPT = r"""#!/bin/sh
records={records}
witness={witness}
grandparent=$(/bin/ps -o ppid= -p "$PPID" | /usr/bin/tr -d ' ')
echo "$$ $PPID ${{grandparent:-0}}" >> "$witness/runs"
for record in "$records"/*.json; do
    if [ -e "$record" ] && /usr/bin/grep -Eq "\"supervisor_pid\": ($PPID|$grandparent),\$" "$record"; then
        /bin/cp "$record" "$witness/$$.json"
    fi
done
{body}
"""
"""The stand-in program: the log of its run and the copy of its record, then the body the test gave it."""


@dataclass(frozen=True)
class StandInRun:
    """One run of a stand-in, as the run logged itself."""

    pid: int
    """the run's process id."""
    parent_pid: int
    """the process id of the run's parent."""
    grandparent_pid: int
    """the process id of the parent of the run's parent."""
    record: dict[str, Any] | None
    """the brain-supervise record whose supervisor is the run's parent or grandparent, as the run found it, or None when
    it found none."""

    @property
    def supervised(self) -> bool:
        """
        :return: whether the run found a record of brain-supervise whose supervisor is the run's parent, or its
            grandparent where a shell started it.
        """
        if self.record is None:
            return False
        return self.record.get("owner") == supervise.OWNER and self.record.get("supervisor_pid") in (self.parent_pid, self.grandparent_pid)


class StandIn:
    """An executable named as a program a function runs, which logs each of its runs."""

    def __init__(self, path: Path, witness: Path) -> None:
        """
        :param path: the executable.
        :param witness: the directory each run logs itself in.
        """
        self.path = path
        self._witness = witness

    def runs(self) -> list[StandInRun]:
        """
        :return: each run of the stand-in so far, in the order the runs logged themselves.
        """
        log = self._witness / "runs"
        if not log.exists():
            return []
        runs = []
        for line in log.read_text().splitlines():
            pid, parent_pid, grandparent_pid = (int(field) for field in line.split())
            record_path = self._witness / f"{pid}.json"
            record = json.loads(record_path.read_text()) if record_path.exists() else None
            runs.append(StandInRun(pid, parent_pid, grandparent_pid, record))
        return runs


class StandIns:
    """The stand-ins of one test, alone on PATH, with the test's own restart root."""

    def __init__(self, directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        :param directory: the test's directory, which holds the stand-ins, the restart root and the logs of the runs.
        :param monkeypatch: the test's monkeypatch, through which PATH and the restart root are set for the test.
        """
        # the directories: the stand-ins', the restart root and the runs' logs
        self.bin = directory / "bin"
        self.bin.mkdir(parents=True)
        self.root = directory / "restart"
        self._records = self.root / supervise.OWNER / "process"
        self._witness = directory / "witness"
        self._witness.mkdir()

        # the environment: the restart root, and a PATH of the stand-ins alone
        monkeypatch.setenv(supervise.ROOT_ENV, str(self.root))
        monkeypatch.setenv("PATH", str(self.bin))

    def add(self, name: str, body: str) -> StandIn:
        """
        :param name: the program's name.
        :param body: the shell the stand-in runs once it has logged its run, with the program's arguments in ``$@``.
        :return: the stand-in.
        """
        witness = self._witness / name
        witness.mkdir()
        path = self.bin / name
        path.write_text(
            _SCRIPT.format(
                records=shlex.quote(str(self._records)),
                witness=shlex.quote(str(witness)),
                body=body,
            )
        )
        path.chmod(0o755)
        return StandIn(path, witness)

    def answering(self, name: str, stdout: str = "", stderr: str = "", exit_code: int = 0) -> StandIn:
        """
        :param name: the program's name.
        :param stdout: what the stand-in writes to its stdout.
        :param stderr: what the stand-in writes to its stderr.
        :param exit_code: the stand-in's exit code.
        :return: a stand-in that answers every run alike.
        """
        answers = self.bin.parent / "answers" / name
        answers.mkdir(parents=True)
        (answers / "stdout").write_text(stdout)
        (answers / "stderr").write_text(stderr)
        quoted = shlex.quote(str(answers))
        return self.add(name, f"/bin/cat {quoted}/stdout\n/bin/cat {quoted}/stderr >&2\nexit {exit_code}")

    def unrunnable(self, name: str) -> Path:
        """
        :param name: the program's name.
        :return: an executable named as the program whose interpreter does not exist, so that PATH finds the program and
            starting it fails.
        """
        path = self.bin / name
        path.write_text("#!/nonexistent/interpreter\n")
        path.chmod(0o755)
        return path


@dataclass(frozen=True)
class TimedOutRun:
    """One run that :func:`time_out_runs` made time out."""

    args: str | Sequence[str]
    """the command of the run, as :func:`solidlsp.util.supervise.run` was given it."""
    timeout: float | None
    """the timeout of the run in seconds, as :func:`solidlsp.util.supervise.run` was given it."""


def time_out_runs(monkeypatch: pytest.MonkeyPatch, matching: Callable[[Sequence[str]], bool] = lambda args: True) -> list[TimedOutRun]:
    """
    Makes each matching run time out as soon as it is asked for, for the handler of a timeout that the test would
    otherwise wait out. A run that does not match is started as it is.

    :param monkeypatch: the test's monkeypatch, through which :func:`solidlsp.util.supervise.run` is replaced for the test.
    :param matching: whether the command of a run is one that times out.
    :return: the runs that timed out, in the order they were asked for, which grows as the test runs.
    """
    real_run = supervise.run
    timed_out: list[TimedOutRun] = []

    def run(args: str | Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
        if not matching(args):
            return real_run(args, **kwargs)
        timed_out.append(TimedOutRun(args, kwargs.get("timeout")))
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout") or 0)

    monkeypatch.setattr(supervise, "run", run)
    return timed_out


def assert_started_through_brain_supervise(*stand_ins: StandIn) -> None:
    """
    :param stand_ins: the stand-ins of the programs a function ran.
    :raises AssertionError: unless each stand-in ran and every run of it found the brain-supervise record that names it.
    """
    for stand_in in stand_ins:
        runs = stand_in.runs()
        assert runs, f"{stand_in.path.name} never ran"
        unsupervised = [run for run in runs if not run.supervised]
        assert not unsupervised, f"{stand_in.path.name} ran without a brain-supervise record: {unsupervised}"


class RecordWatcher:
    """
    Every brain-supervise record that appears under a restart root while the watcher runs, for a program that cannot
    be stood in for on PATH because it is started by its absolute path. A record lives from before its process starts
    until the starter has read the process's outcome, so a watcher that looks every half millisecond sees it.
    """

    _INTERVAL_SECONDS = 0.0005

    def __init__(self, root: Path) -> None:
        """
        :param root: the restart root.
        """
        self._records = root / supervise.OWNER / "process"
        self._seen: dict[str, dict[str, Any]] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, name="record-watcher", daemon=True)

    def __enter__(self) -> "RecordWatcher":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join()

    def records(self) -> list[dict[str, Any]]:
        """
        :return: the last state seen of each record that appeared.
        """
        return list(self._seen.values())

    def _watch(self) -> None:
        while not self._stop.is_set():
            for path in self._records.glob("*.json"):
                try:
                    self._seen[path.name] = json.loads(path.read_text())
                except (FileNotFoundError, json.JSONDecodeError):
                    pass
            time.sleep(self._INTERVAL_SECONDS)
