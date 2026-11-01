"""
The Lean 4 family starts ``lake`` through brain-supervise: the launch environment of
:class:`Lean4LanguageServer.DependencyProvider`, run against a stand-in on PATH.
"""

import os
from pathlib import Path

import pytest

from solidlsp.language_servers.lean4_language_server import Lean4LanguageServer
from solidlsp.settings import SolidLSPSettings
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


def test_the_launch_environment_holds_the_lean_paths_lake_env_prints(stand_ins: StandIns, tmp_path: Path) -> None:
    lake = stand_ins.answering(
        "lake",
        stdout="ELAN_TOOLCHAIN=leanprover/lean4:v4.9.0\nLEAN_PATH=/project/.lake/build/lib\nLEAN_SRC_PATH=/project/src\n",
    )
    project = tmp_path / "project"
    project.mkdir()
    provider = Lean4LanguageServer.DependencyProvider(SolidLSPSettings.CustomLSSettings({}), str(tmp_path / "resources"), str(project))

    assert provider.create_launch_command_env() == {"LEAN_PATH": "/project/.lake/build/lib", "LEAN_SRC_PATH": "/project/src"}
    assert_started_through_brain_supervise(lake)
