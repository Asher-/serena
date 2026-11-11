"""
The stdin of a process :mod:`solidlsp.util.supervise` starts, against the real brain-supervise program named by
BRAIN_SUPERVISE: a message larger than the process's FIFO holds reaches a process that waits on it as node does, and a
write to a process that has ended raises rather than waiting for a reader.
"""

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from solidlsp.util import supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")

_NODE = shutil.which("node")

_MESSAGE_BYTES = 50_000
"""the size of a message: a document's text as a language server is told it, several times what a FIFO holds."""


@pytest.fixture(autouse=True)
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The records' root, whose processes are killed after the test whatever it left running."""
    directory = tmp_path / "restart"
    monkeypatch.setenv(supervise.ROOT_ENV, str(directory))
    yield directory
    for path in (directory / supervise.OWNER / "process").glob("*.json"):
        if "ended_at" not in json.loads(path.read_text()):
            subprocess.run([supervise.program(), "stop", "-signal", "SIGKILL", str(path)], check=False, stderr=subprocess.DEVNULL)


def _write_within(stream: Any, data: bytes, seconds: float) -> BaseException | None:
    """
    :return: what the write and flush raised, or None; the test fails when they have not returned within the seconds.
    """
    raised: list[BaseException | None] = []

    def write() -> None:
        try:
            stream.write(data)
            stream.flush()
            raised.append(None)
        except BaseException as e:
            raised.append(e)

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    writer.join(seconds)
    assert raised, f"the write of {len(data)} bytes did not return within {seconds} s"
    return raised[0]


@pytest.mark.skipif(_NODE is None, reason="no node on the path")
def test_a_node_process_reads_a_message_larger_than_its_fifo_holds() -> None:
    script = f"let n=0;process.stdin.on('data',d=>{{n+=d.length;if(n>={_MESSAGE_BYTES}){{console.log(n);process.exit(0)}}}})"
    process = supervise.Popen([_NODE, "-e", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert process.stdin is not None and process.stdout is not None

    assert _write_within(process.stdin, b"x" * _MESSAGE_BYTES, 30) is None
    assert process.stdout.readline() == f"{_MESSAGE_BYTES}\n".encode()
    assert process.wait(30) == 0
    process.close_streams()


def test_a_write_to_an_adopted_process_that_has_ended_raises_a_broken_pipe() -> None:
    process = supervise.Popen(["/bin/sh", "-c", "read line"], stdin=subprocess.PIPE, keep=True)
    assert process.stdin is not None
    process.stdin.write(b"line\n")
    process.stdin.flush()
    assert process.wait(30) == 0
    process.close_streams()
    adopted = supervise.Popen.adopt(process.record)
    assert adopted.stdin is not None

    assert isinstance(_write_within(adopted.stdin, b"x" * _MESSAGE_BYTES, 30), BrokenPipeError)
    adopted.close_streams()
    adopted.release()
