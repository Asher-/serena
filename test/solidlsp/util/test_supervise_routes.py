"""
The routes of :mod:`solidlsp.util.supervise` no other test takes, each against the real brain-supervise program named by
BRAIN_SUPERVISE: a stdin and an output given as descriptors, a start the supervisor refuses, a FIFO an adopter finds gone
or unread, a released record, a signal to an ended process, the start time of no process, a reclaim that frees nothing,
and the records startup adoption cannot take up or whose call it cannot answer.
"""

import ctypes
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import psutil
import pytest

from serena.call_journal import CallJournal, CallResult
from solidlsp.util import supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


@pytest.fixture(autouse=True)
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The records' root, whose processes are killed after the test whatever it left running."""
    directory = tmp_path / "restart"
    monkeypatch.setenv(supervise.ROOT_ENV, str(directory))
    yield directory
    for path in (directory / supervise.OWNER / "process").glob("*.json"):
        if "ended_at" not in json.loads(path.read_text()):
            subprocess.run([supervise.program(), "stop", "-signal", "SIGKILL", str(path)], check=False, stderr=subprocess.DEVNULL)


def _in_frame(thread: threading.Thread, function: str) -> bool:
    """:return: whether the thread is running the function, which it is observed to be in, not assumed to be."""
    frame = sys._current_frames().get(thread.ident or 0)
    while frame is not None:
        if frame.f_code.co_name == function:
            return True
        frame = frame.f_back
    return False


def _await(condition: Any, what: str) -> None:
    deadline = time.monotonic() + 60
    while not condition():
        assert time.monotonic() < deadline, what
        time.sleep(0.001)


def test_a_process_reads_the_file_given_as_its_stdin(tmp_path: Path) -> None:
    given = tmp_path / "given"
    given.write_bytes(b"from the file\n")
    with given.open("rb") as stdin:
        done = supervise.run(["/bin/cat"], stdin=stdin, capture_output=True)
    assert done.stdout == b"from the file\n"


def test_stderr_given_as_stdout_follows_stdout_into_the_file_given(tmp_path: Path) -> None:
    output = tmp_path / "output"
    with output.open("wb") as stdout:
        supervise.run(["/bin/sh", "-c", "echo a; echo b >&2"], stdout=stdout, stderr=subprocess.STDOUT)
    assert output.read_bytes() == b"a\nb\n"


def test_output_given_as_devnull_is_discarded_and_kept_in_no_file_or_stream(capfd: pytest.CaptureFixture[str]) -> None:
    completed = supervise.run(["/bin/sh", "-c", "echo a; echo b >&2"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    assert (completed.returncode, completed.stdout, completed.stderr) == (0, None, None)
    captured = capfd.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_a_start_the_supervisor_cannot_make_raises_the_error_it_reports(tmp_path: Path) -> None:
    # a directory that is there and cannot be entered: the supervisor's start of the process fails, and it says why
    closed = tmp_path / "closed"
    closed.mkdir(mode=0)
    try:
        with pytest.raises(OSError, match="permission denied"):
            supervise.Popen(["/bin/echo"], cwd=closed)
    finally:
        closed.chmod(0o700)


def test_a_supervisor_that_reports_nothing_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # a program named as the supervisor that ends without the report brain-supervise gives
    monkeypatch.setenv(supervise.PROGRAM_ENV, "/usr/bin/true")
    with pytest.raises(OSError, match="reported nothing"):
        supervise.Popen(["/bin/echo"])


def test_a_supervisor_that_ended_before_it_read_what_to_start_breaks_no_pipe_into_the_caller() -> None:
    read_end, write_end = os.pipe()
    os.close(read_end)
    with os.fdopen(write_end, "wb") as stdin:
        supervise._give_spec(stdin, {"argv": ["/bin/echo"]})
        assert stdin.closed


def test_adopting_a_process_whose_fifo_is_gone_raises() -> None:
    process = supervise.Popen(["/bin/sleep", "300"], stdin=subprocess.PIPE, keep=True)
    Path(supervise.Record.load(process.record).path.with_suffix(".in")).unlink()

    with pytest.raises(FileNotFoundError):
        supervise.Popen.adopt(process.record)
    process.kill()
    process.wait()


def test_an_adopter_waits_for_a_reader_of_the_fifo_while_the_process_runs_and_not_once_it_has_ended(tmp_path: Path) -> None:
    # a process that closes its stdin once its starter has written to it, so no reader of its FIFO is left, and runs until told to end
    go = tmp_path / "go"
    script = f'read line; exec 0<&-; echo closed; while [ ! -e "{go}" ]; do sleep 0.01; done'
    process = supervise.Popen(["/bin/sh", "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, keep=True)
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(b"written\n")
    process.stdin.flush()
    assert process.stdout.readline() == b"closed\n"
    adopted: list[supervise.Popen] = []
    adopter = threading.Thread(target=lambda: adopted.append(supervise.Popen.adopt(process.record)))
    adopter.start()

    _await(lambda: _in_frame(adopter, "_open_fifo"), "the adopter did not wait on the FIFO")
    assert not adopted
    go.touch()
    adopter.join(60)

    assert adopted and adopted[0].poll() == 0
    assert adopted[0].stdin is not None
    adopted[0].close_streams()
    adopted[0].release()


def test_a_released_record_leaves_a_running_process_unknown_to_poll() -> None:
    process = supervise.Popen(["/bin/sleep", "300"], keep=True)
    process.release()

    assert process.poll() is None
    os.kill(process.pid, signal.SIGKILL)


def test_a_signal_to_a_process_that_has_ended_is_not_sent() -> None:
    process = supervise.Popen(["/bin/sh", "-c", "exit 5"], keep=True)
    assert process.wait() == 5

    process.terminate()

    assert process.poll() == 5
    process.release()


def test_no_pid_a_pid_no_process_has_and_a_zombie_have_no_start_time() -> None:
    zombie = subprocess.Popen(["/usr/bin/true"])
    _await(lambda: psutil.Process(zombie.pid).status() == psutil.STATUS_ZOMBIE, "the child did not end")
    ended = subprocess.Popen(["/usr/bin/true"])
    ended.wait()

    assert (supervise.start_time(0), supervise.start_time(ended.pid), supervise.start_time(zombie.pid)) == ("", "", "")
    zombie.wait()


def test_a_start_time_whose_microseconds_round_up_carries_into_the_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(psutil.Process, "create_time", lambda _self: 1793499747.9999997)
    assert supervise.start_time(os.getpid()) == "1793499748.000000"


def test_a_reclaim_of_less_than_a_block_frees_nothing_and_one_a_device_refuses_leaves_it(tmp_path: Path) -> None:
    stream = tmp_path / "stream"
    stream.write_bytes(b"x" * 100)

    supervise.reclaim(stream, 99)
    supervise.reclaim(os.devnull, 1 << 22)

    assert stream.read_bytes() == b"x" * 100


def test_on_linux_a_start_time_is_the_field_of_stat_after_a_command_name_with_spaces_and_a_zombie_or_no_process_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the kernel's own text in the format proc(5) gives, since this host has no /proc: the 22nd field is the start time
    fields = "1 101 101 0 -1 4194304 1 0 0 0 0 0 0 0 20 0 1 0 987654 1000 1"
    stats = {"/proc/101/stat": f"101 (a b) c) S {fields}\n", "/proc/102/stat": f"102 (zombie) Z {fields}\n"}

    def read_text(path: Path) -> str:
        if str(path) not in stats:
            raise FileNotFoundError(path)
        return stats[str(path)]

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "read_text", read_text)

    assert (supervise.start_time(101), supervise.start_time(102), supervise.start_time(103)) == ("987654", "", "")


def test_on_linux_a_reclaim_punches_out_the_whole_blocks_and_a_refusal_leaves_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # the kernel's call stands in, since this host has no fallocate: what is asserted is the call serena makes of it
    stream = tmp_path / "stream"
    stream.write_bytes(b"x" * 100)
    block = os.statvfs(stream).f_frsize
    calls: list[tuple[int, int, int]] = []
    answers = [0, -1]

    class Libc:
        def fallocate(self, _fd: int, mode: int, offset: ctypes.c_int64, length: ctypes.c_int64) -> int:
            calls.append((mode, offset.value, length.value))
            return answers.pop(0)

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ctypes, "CDLL", lambda *_args, **_kwargs: Libc())

    with caplog.at_level(logging.DEBUG, logger=supervise.__name__):
        supervise.reclaim(stream, block * 2 + 5)
        assert "Could not free" not in caplog.text
        supervise.reclaim(stream, block)

    assert calls == [(supervise._FALLOC_PUNCH_HOLE, 0, block * 2), (supervise._FALLOC_PUNCH_HOLE, 0, block)]
    assert "Could not free the consumed prefix" in caplog.text
    assert stream.read_bytes() == b"x" * 100


class _Echo(supervise.Continuation):
    """The call's result is the process's exit code, or the error that computing it raised."""

    def __init__(self, fail: bool = False) -> None:
        self._fail = fail

    def result(self, state: Any, record: supervise.Record) -> Any:
        if self._fail:
            raise ValueError("no result")
        return CallResult(str(record.outcome().returncode), False)

    def failure(self, error: Exception) -> Any:
        return CallResult(str(error), True)


def _left(root: Path, call_id: str, kind: str, command: str) -> supervise.Popen:
    """:return: a process started for the call as an ended instance would have left it, its starter named as no live instance."""
    process = supervise.Popen(["/bin/sh", "-c", command], call_id=call_id, waiting=supervise.Waiting(kind, None, 1, "1.000000"), keep=True)
    return process


def test_startup_adoption_answers_a_call_by_its_continuation_or_its_failure_and_skips_what_it_cannot_take_up(
    root: Path, tmp_path: Path
) -> None:
    go = tmp_path / "go"
    wait = f'while [ ! -e "{go}" ]; do sleep 0.01; done'
    journal = CallJournal()
    journal.receive("answered", "a_tool", {})
    journal.receive("failing", "a_tool", {})
    journal.receive("removed", "a_tool", {})
    continuations = supervise.Continuations()
    continuations.register("echo", _Echo())
    continuations.register("failing", _Echo(fail=True))
    answered = _left(root, "answered", "echo", f"{wait}; exit 4")
    failing = _left(root, "failing", "failing", wait)
    removed = _left(root, "removed", "echo", wait)
    callless = _left(root, "", "", wait)
    malformed = root / supervise.OWNER / "process" / "malformed.json"
    malformed.write_text(json.dumps({"owner": supervise.OWNER, "id": "malformed", "pid": "not a pid", "start": "1.000000"}))

    watchers = supervise.Adoption(journal, (), (), continuations).adopt()
    (root / supervise.OWNER / "call" / "removed.json").unlink()
    go.touch()
    for watcher in watchers:
        watcher.join(60)

    assert journal.resume("answered") == CallResult("4", False)
    assert journal.resume("failing") == CallResult("no result", True)
    assert [path.name for path in (root / supervise.OWNER / "process").glob("*.json")] == ["malformed.json"]
    assert all(not process.record.exists() for process in (answered, failing, removed, callless))


def test_communicate_writes_its_input_to_the_fifo_and_keeps_a_kept_record() -> None:
    process = supervise.Popen(["/usr/bin/head", "-n", "1"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, keep=True)

    assert process.communicate(b"line\nrest\n") == (b"line\n", None)
    assert process.record.exists()
    process.release()


def test_no_program_named_and_none_on_the_path_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(supervise.PROGRAM_ENV)
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(FileNotFoundError, match=supervise.PROGRAM_ENV):
        supervise.program()


def test_a_program_on_the_path_is_found_where_none_is_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    found = tmp_path / "brain-supervise"
    found.write_text("#!/bin/sh\n")
    found.chmod(0o755)
    monkeypatch.delenv(supervise.PROGRAM_ENV)
    monkeypatch.setenv("PATH", str(tmp_path))

    assert supervise.program() == str(found)


def test_the_records_leave_out_one_that_cannot_be_read_and_one_whose_outcome_is_consumed() -> None:
    process = supervise.Popen(["/bin/sleep", "300"], waiting=supervise.Waiting.of("test"), keep=True)
    directory = process.record.parent
    torn = directory / "torn.json"
    torn.write_text("{")
    gone = directory / "gone.json"
    gone.symlink_to(directory / "nowhere.json")
    consumed = directory / "consumed.json"
    consumed.write_text(json.dumps({**json.loads(process.record.read_text()), "consumed": True}))

    found = [record.path for record in supervise.records()]

    # the fixture's sweep reads every record it finds
    for path in (torn, gone, consumed):
        path.unlink()
    assert found == [process.record]


def test_a_stream_that_is_gone_or_no_file_reads_as_nothing_and_a_record_that_is_gone_holds_an_end(tmp_path: Path) -> None:
    record = supervise.Record(tmp_path / "gone.json", "", supervise.Waiting(), "", str(tmp_path / "gone.err"))

    assert record.read(record.stdout) == b""
    assert record.read(record.stderr) == b""
    assert record.outcome() == supervise.Outcome(True, -1)
    assert record.wait() == supervise.Outcome(True, -1)


def test_waiting_fields_that_describe_nothing_are_nothing_waiting() -> None:
    assert supervise.Waiting.from_fields(None) == supervise.Waiting()


def test_a_stop_ends_a_process_by_its_terminate_signal() -> None:
    process = supervise.Popen(["/bin/sleep", "300"], keep=True)

    outcome = supervise.Record.load(process.record).stop()

    assert outcome.ended and outcome.returncode == -signal.SIGTERM


def test_a_stop_kills_a_process_deaf_to_its_terminate_signal() -> None:
    process = supervise.Popen(["/bin/sh", "-c", "trap '' TERM; echo ready; exec sleep 300"], stdout=subprocess.PIPE, keep=True)
    assert process.stdout is not None
    assert process.stdout.readline() == b"ready\n"

    outcome = supervise.Record.load(process.record).stop()

    assert outcome.ended and outcome.returncode == -signal.SIGKILL
