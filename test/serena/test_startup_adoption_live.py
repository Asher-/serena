"""
Serena's startup adoption, live: a real ``serena start-mcp-server`` over a fixture project with a real language server,
spoken to over streamable HTTP as the multiplexer speaks to it. A language server of a project the next instance never
activates is that instance's from its startup (pg://brain/daemon/restart/readoption), and a structural bridge outlives a
terminate of serena, as :meth:`StructuralBridge.close` says, and is the next instance's from its startup.
"""

import json
import os
import shutil
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path
from secrets import token_hex
from typing import Any

import pytest

from serena.call_journal import daemon_owner
from solidlsp.ls_survival import LanguageServerStates
from solidlsp.structural.bridge_process import BRIDGE_KIND, BridgeStates
from solidlsp.util import supervise
from test.conftest import SCRATCH_ROOT
from test.serena.test_restart_survival import _FIXTURE, _language_servers, _running, _Serena, _Started

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")

SCRATCH = SCRATCH_ROOT
"""The directory under which each test here keeps its files, never a system temp directory."""

_RUBY = 'class Greeter\n  def greet(name)\n    "hello #{name}"\n  end\nend\n'


@pytest.fixture
def scratch(request: pytest.FixtureRequest) -> Iterator[Path]:
    directory = SCRATCH / f"{request.node.name[:48]}-{token_hex(4)}"
    directory.mkdir(parents=True)
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def root(scratch: Path, port: int, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """
    The instances' root, whose processes are stopped after the test whatever it left running. Its records are those of
    the owner of a serena listening on the test's port, which this process's records name too.
    """
    monkeypatch.setattr(supervise, "OWNER", daemon_owner(port))
    directory = scratch / "restart"
    directory.mkdir()
    yield directory
    for path in (directory / supervise.OWNER / "process").glob("*.json"):
        if "ended_at" not in json.loads(path.read_text()):
            subprocess.run([supervise.program(), "stop", "-signal", "SIGKILL", str(path)], check=False, stderr=subprocess.DEVNULL)


@pytest.fixture
def project(scratch: Path) -> Path:
    copied = scratch / "project"
    shutil.copytree(_FIXTURE, copied)
    (copied / "greeter.rb").write_text(_RUBY)
    return copied


@pytest.fixture
def instances(root: Path, port: int, scratch: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setenv(supervise.ROOT_ENV, str(root))
    started: list[_Serena] = []

    def start() -> _Serena:
        instance = _Serena(port, root, scratch / "serena.log")
        started.append(instance)
        return instance

    yield start
    for instance in started:
        instance.close()


def _bridges(root: Path) -> list[_Started]:
    started = []
    for path in (root / supervise.OWNER / "process").glob("*.json"):
        fields = json.loads(path.read_text())
        if (fields.get("waiting") or {}).get("kind") == BRIDGE_KIND and "ended_at" not in fields:
            started.append(_Started(path, fields["pid"], fields["start"]))
    return started


def _holders(states: type[LanguageServerStates] | type[BridgeStates]) -> list[int]:
    return [json.loads(path.read_text())["instance_pid"] for path in sorted(states.kind_directory().rglob("*.json"))]


def test_a_language_server_of_a_project_the_next_instance_never_activates_is_that_instances_from_its_startup(
    instances: Any, project: Path, root: Path
) -> None:
    first = instances()
    first.activate(project)
    first.overview()
    servers = _language_servers(root)
    assert servers, "serena started no language server"
    first.kill()

    second = instances()

    assert _holders(LanguageServerStates) == [second.process.pid] * len(servers)
    assert all(_running(server) for server in servers)


def test_a_bridge_outlives_a_terminate_of_serena_and_is_the_next_instances_from_its_startup(
    instances: Any, project: Path, root: Path
) -> None:
    first = instances()
    first.activate(project)
    result = first.call("cursor_overview", {"relative_path": "greeter.rb", "because": "to start the ruby bridge"})
    assert not result.get("isError"), result
    bridges = _bridges(root)
    assert bridges, "serena started no bridge"

    first.terminate()
    assert all(_running(bridge) for bridge in bridges), "a bridge ended with serena"

    second = instances()
    assert _holders(BridgeStates) == [second.process.pid] * len(bridges)
    assert all(_running(bridge) for bridge in bridges)

    # the first use attaches to the real bridge held since startup, which answers the adoption's request of a name no
    # bridge knows and then this call, and no second bridge is started
    second.activate(project)
    result = second.call("cursor_overview", {"relative_path": "greeter.rb", "because": "to use the adopted ruby bridge"})
    assert not result.get("isError"), result
    assert "Greeter" in result["content"][0]["text"], result
    assert sorted(bridge.pid for bridge in _bridges(root)) == sorted(bridge.pid for bridge in bridges)
