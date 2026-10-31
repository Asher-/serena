"""
The bridge process: :mod:`solidlsp.structural.bridge_process` against the real brain-supervise program, named by
BRAIN_SUPERVISE, with a stand-in bridge that speaks the frame protocol every real bridge speaks. A bridge outlives the
instance that started it, which is what each ``_Instance`` here exists to show.
"""

import json
import os
import signal
import struct
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path

import psutil
import pytest

from solidlsp.structural import bridge_process
from solidlsp.structural.bridge_process import BridgeState, BridgeStates, StructuralBridge
from solidlsp.structural.errors import ParseError
from solidlsp.util import supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")

_STAND_IN = textwrap.dedent(
    """
    import json, os, signal, struct, sys, time

    def on_term(*_):
        with open(sys.argv[1], "w") as marker:
            marker.write("terminated")
        sys.exit(0)

    signal.signal(signal.SIGTERM, on_term)
    while True:
        prefix = os.read(0, 4)
        if len(prefix) < 4:
            break
        (length,) = struct.unpack("<I", prefix)
        body = b""
        while len(body) < length:
            chunk = os.read(0, length - len(body))
            if not chunk:
                sys.exit(1)
            body += chunk
        request = json.loads(body)
        op = request["op"]
        if op == "echo":
            response = {"value": request["value"], "pid": os.getpid()}
        elif op == "slow":
            time.sleep(request["seconds"])
            response = {"value": "slow answer", "pid": os.getpid()}
        elif op == "die":
            sys.exit(3)
        else:
            response = {"error": {"kind": "unknown_op", "message": "unknown op: " + op}}
        encoded = json.dumps(response).encode()
        os.write(1, struct.pack("<I", len(encoded)) + encoded)
    """
)

_INSTANCE = textwrap.dedent(
    """
    import json, sys, threading, time
    from solidlsp.structural.bridge_process import StructuralBridge

    bridges = [StructuralBridge(sys.argv[1:], "stand-in") for _ in range(int(sys.stdin.readline()))]
    for bridge in bridges:
        print(bridge.call("echo", value="first")["pid"], flush=True)
    for line in sys.stdin:
        command = json.loads(line)
        if command["do"] == "slow":
            threading.Thread(target=bridges[0].call, args=("slow",), kwargs={"seconds": command["seconds"]}, daemon=True).start()
            time.sleep(0.2)
        elif command["do"] == "torn":
            bridges[0]._process.stdin.write(b"\\xff\\xff\\x00\\x00partial")
            bridges[0]._process.stdin.flush()
        print("done", flush=True)
    """
)


class _Instance:
    """A serena instance of its own, in a process group of its own so that the group can be killed as serena's is."""

    def __init__(self, command: list[str], bridges: int = 1) -> None:
        self._process = subprocess.Popen(
            [sys.executable, "-c", _INSTANCE, *command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        assert self._process.stdin is not None and self._process.stdout is not None
        self._process.stdin.write(f"{bridges}\n")
        self._process.stdin.flush()
        self.pids = [int(self._process.stdout.readline()) for _ in range(bridges)]

    def tell(self, **command: object) -> None:
        assert self._process.stdin is not None and self._process.stdout is not None
        self._process.stdin.write(json.dumps(command) + "\n")
        self._process.stdin.flush()
        assert self._process.stdout.readline() == "done\n"

    def kill(self) -> None:
        os.killpg(self._process.pid, signal.SIGKILL)
        self._process.wait()


@pytest.fixture(autouse=True)
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(supervise.ROOT_ENV, str(tmp_path))
    return tmp_path


@pytest.fixture
def marker(tmp_path: Path) -> Path:
    return tmp_path / "terminated"


@pytest.fixture
def command(tmp_path: Path, marker: Path) -> Iterator[list[str]]:
    """The stand-in's command, whose bridges are stopped after the test whatever it left running."""
    script = tmp_path / "stand_in.py"
    script.write_text(_STAND_IN)
    yield [sys.executable, str(script), str(marker)]
    for path in (tmp_path / supervise.OWNER / "process").glob("*.json"):
        fields = json.loads(path.read_text())
        if "ended_at" not in fields and fields.get("pid"):
            subprocess.run([supervise.program(), "stop", "-signal", "SIGKILL", str(path)], check=False, stderr=subprocess.DEVNULL)


def _running(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _await_end(pid: int) -> None:
    deadline = time.monotonic() + 10
    while _running(pid):
        assert time.monotonic() < deadline, f"process {pid} is still running"
        time.sleep(0.01)


def test_a_call_is_answered_and_its_bridge_is_started_once(command: list[str]) -> None:
    bridge = StructuralBridge(command, "stand-in")

    first = bridge.call("echo", value="one")
    second = bridge.call("echo", value="two")

    assert first["value"] == "one"
    assert second["value"] == "two"
    assert first["pid"] == second["pid"]
    bridge.close()


def test_close_stops_the_bridge_by_its_terminate_signal_and_leaves_nothing_behind(command: list[str], marker: Path, root: Path) -> None:
    bridge = StructuralBridge(command, "stand-in")
    pid = bridge.call("echo", value="one")["pid"]

    bridge.close()

    assert marker.read_text() == "terminated"
    assert not _running(pid)
    assert list((root / supervise.OWNER / "bridge").rglob("*.json")) == []
    assert list((root / supervise.OWNER / "process").glob("*")) == []


def test_a_bridge_that_ended_is_replaced_at_the_next_call(command: list[str]) -> None:
    bridge = StructuralBridge(command, "stand-in")
    first = bridge.call("echo", value="one")["pid"]

    with pytest.raises(ParseError):
        bridge.call("die")
    second = bridge.call("echo", value="two")["pid"]

    assert second != first
    bridge.close()


def test_a_bridge_outlives_a_kill_of_its_instance_and_the_next_instance_adopts_it(command: list[str]) -> None:
    instance = _Instance(command)
    pid = instance.pids[0]
    instance.kill()
    assert _running(pid)

    bridge = StructuralBridge(command, "stand-in")

    assert bridge.call("echo", value="after")["pid"] == pid
    bridge.close()


def test_the_answer_to_a_request_in_flight_at_the_kill_is_not_taken_for_the_next_answer(command: list[str]) -> None:
    instance = _Instance(command)
    instance.tell(do="slow", seconds=0.6)
    instance.kill()

    bridge = StructuralBridge(command, "stand-in")

    assert bridge.call("echo", value="mine")["value"] == "mine"
    bridge.close()


def test_each_of_several_bridges_of_one_command_is_adopted_once(command: list[str]) -> None:
    instance = _Instance(command, bridges=2)
    instance.kill()

    bridges = [StructuralBridge(command, "stand-in") for _ in range(3)]
    pids = [bridge.call("echo", value="after")["pid"] for bridge in bridges]

    assert set(pids[:2]) == set(instance.pids)
    assert pids[2] not in instance.pids
    for bridge in bridges:
        bridge.close()


def test_a_bridge_held_by_a_live_instance_is_not_adopted(command: list[str]) -> None:
    instance = _Instance(command)

    bridge = StructuralBridge(command, "stand-in")

    assert bridge.call("echo", value="mine")["pid"] != instance.pids[0]
    assert _running(instance.pids[0])
    bridge.close()
    instance.kill()


def test_a_bridge_a_request_left_half_written_is_stopped_and_replaced(command: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    instance = _Instance(command)
    instance.tell(do="torn")
    pid = instance.pids[0]
    instance.kill()
    monkeypatch.setattr(bridge_process, "_ADOPTION_SECONDS", 0.5)

    bridge = StructuralBridge(command, "stand-in")

    assert bridge.call("echo", value="mine")["pid"] != pid
    _await_end(pid)
    bridge.close()


def test_a_bridge_that_ended_while_no_instance_ran_leaves_nothing_to_adopt(command: list[str], root: Path) -> None:
    instance = _Instance(command)
    pid = instance.pids[0]
    instance.kill()
    os.kill(pid, signal.SIGKILL)
    _await_end(pid)

    bridge = StructuralBridge(command, "stand-in")

    assert bridge.call("echo", value="mine")["pid"] != pid
    assert len(list((root / supervise.OWNER / "bridge").rglob("*.json"))) == 1
    bridge.close()


def test_of_two_instances_reaching_one_state_file_one_claims_it(
    command: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    states = BridgeStates(command)
    states.save(BridgeState(record=str(tmp_path / "20261031T000000-a.json"), instance_pid=1))
    rival: list[BridgeState | None] = []

    # the rival claims between this instance reading the state and claiming it
    def rival_claims_first(_pid: int) -> bool:
        if not rival:
            rival.append(None)
            rival[0] = BridgeStates(command).claim()
        return False

    monkeypatch.setattr(supervise, "process_exists", rival_claims_first)

    mine = states.claim()

    assert rival[0] is not None
    assert mine is None


def test_the_frame_format_is_a_little_endian_length_and_the_body() -> None:
    # the bridges in src/solidlsp/structural/backends read and write exactly this
    assert bridge_process.frame(b"abc") == struct.pack("<I", 3) + b"abc"
