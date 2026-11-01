"""
The supervised process: :mod:`solidlsp.util.supervise` against the real brain-supervise program, named by
BRAIN_SUPERVISE. A process outlives the end of the process that started it, which is what the test
child exists to show.
"""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from solidlsp.util import supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


@pytest.fixture(autouse=True)
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(supervise.ROOT_ENV, str(tmp_path))
    return tmp_path


def test_run_captures_stdout_stderr_and_the_exit_code() -> None:
    done = supervise.run(["/bin/sh", "-c", "echo out; echo err >&2; exit 3"], capture_output=True, text=True)
    assert (done.returncode, done.stdout, done.stderr) == (3, "out\n", "err\n")


def test_run_raises_for_a_failure_when_checked() -> None:
    with pytest.raises(subprocess.CalledProcessError) as caught:
        supervise.run(["/bin/sh", "-c", "exit 4"], check=True)
    assert caught.value.returncode == 4


def test_run_kills_a_process_past_its_timeout() -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        supervise.run(["/bin/sleep", "30"], timeout=0.2)


def test_run_gives_a_process_its_input_to_the_end() -> None:
    assert supervise.run(["/bin/cat"], input="given", capture_output=True, text=True).stdout == "given"


def test_run_of_a_command_that_is_not_there_raises_as_subprocess_does() -> None:
    with pytest.raises(FileNotFoundError):
        supervise.run(["serena-no-such-command"])


def test_run_in_a_directory_that_is_not_there_raises_as_subprocess_does(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        supervise.run(["/bin/echo"], cwd=tmp_path / "absent")


def test_stderr_given_as_stdout_is_read_with_stdout() -> None:
    done = supervise.run(["/bin/sh", "-c", "echo a; echo b >&2"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert done.stdout == b"a\nb\n"


def test_a_shell_command_runs_under_the_shell_with_the_environment_given(tmp_path: Path) -> None:
    done = supervise.run('echo "$GIVEN" "$(pwd -P)"', shell=True, env={"GIVEN": "yes", "PATH": "/bin"}, cwd=tmp_path, capture_output=True)
    assert done.stdout.decode().split() == ["yes", str(tmp_path.resolve())]


def test_a_finished_run_leaves_no_record(root: Path) -> None:
    supervise.run(["/bin/echo"], capture_output=True)
    assert not list((root / supervise.OWNER / "process").iterdir())


def test_a_process_reads_what_is_written_to_its_fifo_and_a_reader_follows_it() -> None:
    process = supervise.Popen(["/bin/cat"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(b"line\n")
    process.stdin.flush()
    assert process.stdout.readline() == b"line\n"
    process.terminate()
    assert process.wait() == -signal.SIGTERM


def test_a_read_only_stdin_counts_its_bytes_and_reads_no_end_of_file_once_every_other_writer_has_gone(tmp_path: Path) -> None:
    # what FIONREAD says of the stdin, and what a read says once the file below shows this end has closed
    script = textwrap.dedent(
        """
        import fcntl, os, select, struct, sys, termios, time
        select.select([0], [], [])
        print(struct.unpack("i", fcntl.ioctl(0, termios.FIONREAD, bytes(4)))[0], flush=True)
        os.read(0, 100)
        while not os.path.exists(sys.argv[1]):
            time.sleep(0.01)
        os.set_blocking(0, False)
        try:
            print("end of file" if os.read(0, 1) == b"" else "a byte", flush=True)
        except BlockingIOError:
            print("no end of file", flush=True)
        """
    )
    closed = tmp_path / "closed"
    process = supervise.Popen(
        [sys.executable, "-c", script, str(closed)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, read_only_stdin=True
    )
    assert process.stdin is not None and process.stdout is not None

    process.stdin.write("line\n")
    process.stdin.flush()
    assert process.stdout.readline() == "5\n"

    process.stdin.close()
    closed.touch()
    assert process.stdout.readline() == "no end of file\n"
    assert process.wait() == 0


@pytest.mark.skipif(sys.platform != "darwin", reason="the claim observed is macOS's")
def test_on_macos_fionread_counts_no_bytes_of_a_stdin_fifo_opened_read_write_though_a_read_takes_them() -> None:
    # what FIONREAD says of the stdin, the FIFO the supervisor hands the process read-write, and what a read then takes
    script = textwrap.dedent(
        """
        import fcntl, os, select, struct, termios
        select.select([0], [], [])
        print(struct.unpack("i", fcntl.ioctl(0, termios.FIONREAD, bytes(4)))[0], flush=True)
        print(os.read(0, 100).decode().strip(), flush=True)
        """
    )
    process = supervise.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert process.stdin is not None and process.stdout is not None

    process.stdin.write("line\n")
    process.stdin.flush()

    assert process.stdout.readline() == "0\n"
    assert process.stdout.readline() == "line\n"
    assert process.wait() == 0


def test_a_read_only_stdin_keeps_the_record_on_the_process_and_is_adopted_by_the_next(tmp_path: Path) -> None:
    # a starter leading a group of its own, killed with the process reading its FIFO read-only
    script = textwrap.dedent(
        """
        import subprocess, sys, time
        from solidlsp.util import supervise
        process = supervise.Popen(
            ["/bin/cat"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, waiting=supervise.Waiting.of("test"), keep=True, read_only_stdin=True
        )
        process.stdin.write(b"before\\n")
        process.stdin.flush()
        print(process.record, process.stdout.readline().decode().strip(), flush=True)
        time.sleep(300)
        """
    )
    starter = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, start_new_session=True)
    assert starter.stdout is not None
    record_path, seen = starter.stdout.readline().split()
    assert seen == "before"
    record = Path(record_path)
    os.killpg(starter.pid, signal.SIGKILL)
    starter.wait()

    # the next instance reaches the same process through the record's FIFO
    fields = json.loads(record.read_text())
    assert supervise.process_exists(fields["pid"])
    process = supervise.Popen.adopt(record, stdout_offset=len(b"before\n"))
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(b"after\n")
    process.stdin.flush()
    assert process.stdout.readline() == b"after\n"
    process.terminate()
    assert process.wait() == -signal.SIGTERM


def test_a_signal_reaches_a_child_of_the_process() -> None:
    process = supervise.Popen(["/bin/sh", "-c", "sleep 30 & echo $!; wait"], stdout=subprocess.PIPE, text=True)
    assert process.stdout is not None
    child = int(process.stdout.readline())
    process.kill()
    process.wait()
    deadline = time.monotonic() + 10
    while supervise.process_exists(child) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not supervise.process_exists(child)


def test_a_process_outlives_a_kill_of_its_starters_group_and_is_adopted_by_the_next(tmp_path: Path) -> None:
    # a starter leading a group of its own, killed with the process's stdin FIFO and stdout still open
    script = textwrap.dedent(
        """
        import subprocess, sys, time
        from solidlsp.util import supervise
        process = supervise.Popen(["/bin/cat"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, waiting=supervise.Waiting.of("test"), keep=True)
        process.stdin.write(b"before\\n")
        process.stdin.flush()
        print(process.record, process.stdout.readline().decode().strip(), flush=True)
        time.sleep(300)
        """
    )
    starter = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, start_new_session=True)
    assert starter.stdout is not None
    record_path, seen = starter.stdout.readline().split()
    assert seen == "before"
    record = Path(record_path)
    os.killpg(starter.pid, signal.SIGKILL)
    starter.wait()

    # the next instance, reading on from the first byte the ended one did not consume
    fields = json.loads(record.read_text())
    assert supervise.process_exists(fields["pid"])
    process = supervise.Popen.adopt(record, stdout_offset=len(b"before\n"))
    assert process.pid == fields["pid"]
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(b"after\n")
    process.stdin.flush()
    assert process.stdout.readline() == b"after\n"
    process.terminate()
    assert process.wait() == -signal.SIGTERM


def test_a_process_that_ends_at_once_does_not_hold_up_the_open_of_its_fifo() -> None:
    process = supervise.Popen(["/bin/sh", "-c", "exit 7 <&-"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert process.wait() == 7
    process.close_streams()
