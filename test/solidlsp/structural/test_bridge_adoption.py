"""
A bridge an ended instance held is taken over by its state (pg://brain/daemon/restart/readoption): the instance named by
a pid the kernel gave another process holds nothing, every live bridge is owned from the moment serena starts, and the
consumed prefix of a bridge's stdout is freed without moving the frame boundary a next instance reads on from. The bridge
is a stand-in speaking the frame protocol every real bridge speaks; each first instance is a process of its own, killed
with its whole group as serena is.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path
from secrets import token_hex

import pytest

from solidlsp.structural.bridge_process import BridgeStates, StructuralBridge
from solidlsp.util import supervise
from test.conftest import SCRATCH_ROOT

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")

SCRATCH = SCRATCH_ROOT
"""The directory under which each test here keeps its files, never a system temp directory."""

_DEADLINE_SECONDS = 60.0

_STAND_IN = textwrap.dedent(
    """
    import json, os, struct, sys
    while True:
        prefix = os.read(0, 4)
        if len(prefix) < 4:
            break
        (length,) = struct.unpack("<I", prefix)
        body = b""
        while len(body) < length:
            body += os.read(0, length - len(body))
        request = json.loads(body)
        if request["op"] == "echo":
            response = {"value": request["value"], "pid": os.getpid()}
        else:
            response = {"error": {"kind": "unknown_op", "message": "unknown op: " + request["op"]}}
        encoded = json.dumps(response).encode()
        os.write(1, struct.pack("<I", len(encoded)) + encoded)
    """
)

_INSTANCE = textwrap.dedent(
    """
    import sys, time
    from solidlsp.structural.bridge_process import StructuralBridge
    bridge = StructuralBridge(sys.argv[2:], "stand-in")
    for _ in range(int(sys.argv[1])):
        bridge.call("echo", value="x" * 65536)
    print(bridge.call("echo", value="first")["pid"], flush=True)
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


@pytest.fixture
def command(scratch: Path) -> list[str]:
    script = scratch / "stand_in.py"
    script.write_text(_STAND_IN)
    return [sys.executable, str(script)]


def _first_instance(root: Path, command: list[str], large_calls: int = 0) -> int:
    """Runs a first instance whose bridge answers its calls, and kills its whole group; :return: the bridge's pid."""
    instance = subprocess.Popen(
        [sys.executable, "-c", _INSTANCE, str(large_calls), *command],
        env={**os.environ, supervise.ROOT_ENV: str(root)},
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert instance.stdout is not None
    pid = int(instance.stdout.readline())
    os.killpg(instance.pid, signal.SIGKILL)
    instance.wait()
    return pid


def _state_files() -> list[Path]:
    return sorted(BridgeStates.kind_directory().rglob("*.json"))


def _await(condition: object, what: str) -> None:
    deadline = time.monotonic() + _DEADLINE_SECONDS
    while not condition():  # type: ignore[operator]
        assert time.monotonic() < deadline, what
        time.sleep(0.01)


def test_a_bridge_whose_instance_pid_the_kernel_gave_another_process_is_adopted(root: Path, command: list[str]) -> None:
    pid = _first_instance(root, command)

    # the ended instance's pid, now a live process's that started at another time
    [path] = _state_files()
    path.write_text(json.dumps({**json.loads(path.read_text()), "instance_pid": os.getppid()}))
    bridge = StructuralBridge(command, "stand-in")

    assert bridge.call("echo", value="after")["pid"] == pid
    bridge.close()


def test_every_live_bridge_is_owned_from_startup_and_its_first_use_attaches_to_it(root: Path, command: list[str]) -> None:
    pid = _first_instance(root, command)
    [path] = _state_files()

    BridgeStates.hold_everywhere()
    owner = json.loads(path.read_text())["instance_pid"]
    bridge = StructuralBridge(command, "stand-in")

    assert owner == os.getpid()
    assert bridge.call("echo", value="after")["pid"] == pid
    bridge.close()


def test_a_bridge_held_since_startup_that_ends_unused_has_its_outcome_consumed(root: Path, command: list[str]) -> None:
    pid = _first_instance(root, command)
    [path] = _state_files()
    record = Path(json.loads(path.read_text())["record"])
    BridgeStates.hold_everywhere()

    os.kill(pid, signal.SIGKILL)

    _await(lambda: not path.exists() and not record.exists(), "the ended bridge's state and record were not consumed")


def test_the_consumed_prefix_of_a_bridges_stdout_is_freed_and_a_next_instance_reads_on_from_a_frame_boundary(
    root: Path, command: list[str]
) -> None:
    pid = _first_instance(root, command, large_calls=40)
    [path] = _state_files()
    state = json.loads(path.read_text())
    stdout = Path(supervise.Record.load(Path(state["record"])).stdout)

    status = os.stat(stdout)
    assert state["stdout_offset"] > 1 << 20
    assert status.st_blocks * 512 < status.st_size - (1 << 20)

    bridge = StructuralBridge(command, "stand-in")
    answer = bridge.call("echo", value="mine")
    assert (answer["value"], answer["pid"]) == ("mine", pid)
    bridge.close()
