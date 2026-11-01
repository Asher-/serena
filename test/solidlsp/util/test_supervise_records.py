"""
What serena reads of a process record and does with it, against the real brain-supervise program named by
BRAIN_SUPERVISE: the outcome, the liveness of the process a pid and start time name, the call and waiting kind a record
names, a timed-out run's record, and the freeing of a stream file's consumed prefix.
"""

import json
import os
import shutil
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from secrets import token_hex

import psutil
import pytest

from solidlsp.util import supervise
from test.conftest import SCRATCH_ROOT

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")

SCRATCH = SCRATCH_ROOT
"""The directory under which each test here keeps its files, never a system temp directory."""


@pytest.fixture
def scratch(request: pytest.FixtureRequest) -> Iterator[Path]:
    directory = SCRATCH / f"{request.node.name[:48]}-{token_hex(4)}"
    directory.mkdir(parents=True)
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def root(scratch: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The records' root, whose processes are killed after the test whatever it left running."""
    directory = scratch / "restart"
    monkeypatch.setenv(supervise.ROOT_ENV, str(directory))
    yield directory
    for path in (directory / supervise.OWNER / "process").glob("*.json"):
        if "ended_at" not in json.loads(path.read_text()):
            subprocess.run([supervise.program(), "stop", "-signal", "SIGKILL", str(path)], check=False, stderr=subprocess.DEVNULL)


def _write_record(path: Path, **fields: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"owner": supervise.OWNER, "id": path.stem, **fields}))
    return path


def test_the_outcome_is_read_again_when_the_supervisor_records_it_and_exits_between_the_read_and_the_checks(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _write_record(root / "r.json", pid=11, start="1.000000", supervisor_pid=12, supervisor_start="2.000000")

    # the supervisor writes the end and exits after the record was read and before it is checked
    def supervisor_ends_now(pid: int, start: str) -> bool:
        if pid == 12:
            fields = json.loads(record.read_text())
            record.write_text(json.dumps({**fields, "ended_at": "2026-10-31T00:00:00Z", "exit_code": 0}))
        return False

    monkeypatch.setattr(supervise, "alive", supervisor_ends_now)

    assert supervise.read_outcome(record) == supervise.Outcome(True, 0)


def test_a_reused_pid_is_not_taken_for_the_process_or_its_supervisor(root: Path) -> None:
    # this test's own pid, which a process with another start time once had
    record = _write_record(root / "r.json", pid=os.getpid(), start="1.000000", supervisor_pid=os.getpid(), supervisor_start="2.000000")

    assert supervise.read_outcome(record) == supervise.Outcome(True, -1)


def test_the_start_time_is_the_one_the_supervisor_records(root: Path) -> None:
    process = supervise.Popen(["/bin/sleep", "300"], keep=True)
    fields = json.loads(process.record.read_text())

    assert supervise.start_time(fields["pid"]) == fields["start"]
    assert supervise.start_time(fields["supervisor_pid"]) == fields["supervisor_start"]
    assert supervise.alive(fields["pid"], fields["start"])
    process.kill()
    process.wait()
    assert not supervise.alive(fields["pid"], fields["start"])
    process.release()


@pytest.mark.skipif(sys.platform != "darwin", reason="the kernel gives a start time in seconds and microseconds on macOS")
def test_the_start_time_keeps_six_digits_of_microseconds_as_the_supervisor_writes_them(monkeypatch: pytest.MonkeyPatch) -> None:
    # a start time whose microseconds end in a zero, which a float's shortest form drops
    monkeypatch.setattr(psutil.Process, "create_time", lambda _self: 1793499747.06825)

    assert supervise.start_time(os.getpid()) == "1793499747.068250"


def test_a_timed_out_run_leaves_no_record_or_stream(root: Path) -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        supervise.run(["/bin/sleep", "300"], timeout=0.2, capture_output=True)

    assert list((root / supervise.OWNER / "process").iterdir()) == []


def test_a_process_started_while_a_call_is_served_names_the_call_and_the_inline_kind(root: Path) -> None:
    token = supervise.CALL_ID.set("call-1")
    try:
        process = supervise.Popen(["/bin/sleep", "300"], keep=True)
        unnamed = supervise.Popen(["/bin/sleep", "300"], keep=True, call_id="")
    finally:
        supervise.CALL_ID.reset(token)

    record = supervise.Record.load(process.record)
    assert (record.call_id, record.waiting.kind) == ("call-1", supervise.INLINE_KIND)
    assert (record.waiting.instance_pid, record.waiting.instance_start) == (os.getpid(), supervise.start_time(os.getpid()))
    assert record.waiting.started_by_a_live_instance()
    other = supervise.Record.load(unnamed.record)
    assert (other.call_id, other.waiting.kind) == ("", "")
    for started in (process, unnamed):
        started.kill()
        started.wait()
        started.release()


def test_the_consumed_prefix_of_a_stream_file_is_freed_and_every_offset_keeps_its_byte(root: Path, scratch: Path) -> None:
    # three megabytes and a little more of a known pattern, written by a supervised process to its stdout file
    script = textwrap.dedent(
        """
        import sys
        sys.stdout.buffer.write(bytes(range(256)) * (3 * 4096 + 1))
        """
    )
    process = supervise.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, keep=True)
    assert process.wait() == 0
    stdout = Path(supervise.Record.load(process.record).stdout)
    written = stdout.read_bytes()
    consumed = 2 * (1 << 20) + 5

    process.reclaim_stdout(consumed)

    status = os.stat(stdout)
    assert status.st_size == len(written)
    assert status.st_blocks * 512 <= len(written) - 2 * (1 << 20) + status.st_blksize
    with stdout.open("rb") as reader:
        reader.seek(consumed)
        assert reader.read() == written[consumed:]
    process.close_streams()
    process.release()


def test_a_record_whose_waiting_holds_fields_serena_does_not_write_is_still_read(root: Path) -> None:
    path = _write_record(root / supervise.OWNER / "process" / "r.json", call_id="c", waiting={"kind": "k", "since": "then"})

    [record] = supervise.records()

    assert (record.path, record.call_id, record.waiting) == (path, "c", supervise.Waiting("k"))
