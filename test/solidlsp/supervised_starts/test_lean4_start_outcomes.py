"""
The Lean 4 family's runs of ``lake`` through brain-supervise, where ``lake`` fails or cannot be started: the launch
environment of :class:`Lean4LanguageServer.DependencyProvider`, run against stand-ins on PATH.
"""

import os
from pathlib import Path

import pytest

from solidlsp.language_servers.lean4_language_server import Lean4LanguageServer
from solidlsp.settings import SolidLSPSettings
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


@pytest.fixture
def provider(tmp_path: Path) -> Lean4LanguageServer.DependencyProvider:
    """The dependency provider of a Lean 4 project."""
    project = tmp_path / "project"
    project.mkdir()
    return Lean4LanguageServer.DependencyProvider(SolidLSPSettings.CustomLSSettings({}), str(tmp_path / "resources"), str(project))


def test_the_launch_environment_is_empty_where_lake_env_fails(
    stand_ins: StandIns, provider: Lean4LanguageServer.DependencyProvider
) -> None:
    lake = stand_ins.answering("lake", stdout="LEAN_PATH=/project/.lake/build/lib\n", stderr="error: no lakefile found\n", exit_code=1)

    assert provider.create_launch_command_env() == {}
    assert_started_through_brain_supervise(lake)


def test_the_launch_environment_is_empty_where_lake_cannot_be_started(
    stand_ins: StandIns, provider: Lean4LanguageServer.DependencyProvider, caplog: pytest.LogCaptureFixture
) -> None:
    stand_ins.unrunnable("lake")

    assert provider.create_launch_command_env() == {}
    assert "Failed to run lake env: supervise: start" in caplog.text
