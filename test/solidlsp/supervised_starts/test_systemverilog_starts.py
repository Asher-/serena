"""
The SystemVerilog family starts ``verible-verilog-ls`` through brain-supervise: the version probe of a system-installed
Verible in :class:`SystemVerilogLanguageServer.DependencyProvider`, run against a stand-in on PATH.
"""

import os
from pathlib import Path

import pytest

from solidlsp.language_servers.systemverilog_server import SystemVerilogLanguageServer
from solidlsp.settings import SolidLSPSettings
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


def test_a_system_verible_is_used_once_its_version_has_been_asked(stand_ins: StandIns, tmp_path: Path) -> None:
    verible = stand_ins.answering("verible-verilog-ls", stdout="Verible v0.0-4051-g9fdb4057 (2024-01-01)\nCommit: 9fdb4057\n")
    provider = SystemVerilogLanguageServer.DependencyProvider(SolidLSPSettings.CustomLSSettings({}), str(tmp_path / "resources"))

    assert provider._get_or_install_core_dependency() == str(verible.path)
    assert_started_through_brain_supervise(verible)
