"""
The dashboard viewer watches no pid of serena's, as a language server is told none
(bug://serena/language_server/initialize/process_id/client/watch), so it outlives serena; the next instance adopts a
live viewer of its dashboard rather than starting a second, and its startup adoption leaves that viewer to it. The viewer
program is a stand-in that records the arguments it was given, run through the real brain-supervise program in place of
the Python interpreter that would open a window.
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

import serena.agent
from serena.agent import SerenaAgent
from serena.call_journal import CallJournal
from serena.dashboard import SerenaDashboardViewer
from solidlsp.util import supervise
from test.conftest import SCRATCH_ROOT

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")

SCRATCH = SCRATCH_ROOT
"""The directory under which each test here keeps its files, never a system temp directory."""

_URL = "http://127.0.0.1:24282/dashboard/index.html"

_PYTHON = sys.executable
"""The interpreter, which a test's agent replaces by the stand-in viewer."""

FIRST_INSTANCE = textwrap.dedent(
    """
    import sys, time
    import serena.agent
    from serena.agent import SerenaAgent
    from serena.dashboard import SerenaDashboardViewer
    sys.executable = sys.argv[1]
    serena.agent.system_has_usable_display = lambda: True
    SerenaDashboardViewer.is_current_platform_supported = staticmethod(lambda: True)
    agent = SerenaAgent.__new__(SerenaAgent)
    agent._dashboard_url = sys.argv[2]
    agent._dashboard_viewer_process = None
    agent._start_dashboard_viewer(minimized=True)
    print(agent._dashboard_viewer_process.pid, flush=True)
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
def viewer(scratch: Path) -> Path:
    """The stand-in viewer program, which writes each argument it was given on a line of its own and runs on."""
    program = scratch / "viewer"
    program.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{scratch}/arguments.$$"\nexec sleep 300\n')
    program.chmod(0o755)
    return program


class _ViewerAgent(SerenaAgent):
    """An agent built with nothing but what starting the dashboard viewer reads, which has nothing to shut down."""

    def __del__(self) -> None:
        pass


@pytest.fixture
def agent(viewer: Path, monkeypatch: pytest.MonkeyPatch) -> SerenaAgent:
    """An agent with only what starting the dashboard viewer reads of it, on a platform with a viewer and a display."""
    monkeypatch.setattr(sys, "executable", str(viewer))
    monkeypatch.setattr(serena.agent, "system_has_usable_display", lambda: True)
    monkeypatch.setattr(SerenaDashboardViewer, "is_current_platform_supported", staticmethod(lambda: True))
    agent = _ViewerAgent.__new__(_ViewerAgent)
    agent._dashboard_url = _URL
    agent._dashboard_viewer_process = None
    return agent


def _live_viewers(root: Path) -> list[supervise.Record]:
    return [record for record in supervise.records() if record.waiting.kind == "serena.dashboard_viewer" and not record.outcome().ended]


def test_the_viewer_is_given_no_pid_of_serenas(agent: SerenaAgent, scratch: Path, root: Path) -> None:
    agent._start_dashboard_viewer(minimized=True)
    process = agent._dashboard_viewer_process
    assert process is not None
    arguments = scratch / f"arguments.{process.pid}"
    deadline = time.monotonic() + 30
    while not arguments.exists() or not arguments.read_text().endswith("\n"):
        assert time.monotonic() < deadline, "the viewer wrote no arguments"
        time.sleep(0.01)

    given = arguments.read_text().splitlines()
    assert _URL in given
    assert str(os.getpid()) not in given


def test_the_browser_is_opened_on_the_url_by_a_process_the_supervisor_started(
    scratch: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = scratch / "opened"
    browser = scratch / "browser"
    browser.write_text(f'#!/bin/sh\nprintf %s "$1" > "{opened}.tmp" && mv "{opened}.tmp" "{opened}"\n')
    browser.chmod(0o755)
    monkeypatch.setenv("BROWSER", str(browser))

    SerenaAgent._open_dashboard_in_browser(_URL)

    deadline = time.monotonic() + 30
    while not opened.exists():
        assert time.monotonic() < deadline, "no browser was opened"
        time.sleep(0.01)
    assert opened.read_text() == _URL
    assert any("webbrowser.open" in path.read_text() for path in (root / supervise.OWNER / "process").glob("*.json"))


def test_a_live_viewer_an_ended_instance_started_is_adopted_and_no_second_is_started(agent: SerenaAgent, viewer: Path, root: Path) -> None:
    instance = subprocess.Popen(
        [_PYTHON, "-c", FIRST_INSTANCE, str(viewer), _URL],
        env={**os.environ, supervise.ROOT_ENV: str(root)},
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert instance.stdout is not None
    first = int(instance.stdout.readline())
    os.killpg(instance.pid, signal.SIGKILL)
    instance.wait()

    agent._start_dashboard_viewer(minimized=True)
    watchers = SerenaAgent.adopt_processes(agent, CallJournal())

    assert agent._dashboard_viewer_process is not None and agent._dashboard_viewer_process.pid == first
    assert len(_live_viewers(root)) == 1
    assert watchers == []
