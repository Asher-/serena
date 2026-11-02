"""
The SystemVerilog family's run of ``verible-verilog-ls`` through brain-supervise, where the version probe of a
system-installed Verible fails or cannot be started: :class:`SystemVerilogLanguageServer.DependencyProvider`, run
against a stand-in on PATH.
"""

import os
from pathlib import Path

import pytest

from solidlsp.language_servers.systemverilog_server import SystemVerilogLanguageServer
from solidlsp.settings import SolidLSPSettings
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


@pytest.fixture
def provider(tmp_path: Path) -> SystemVerilogLanguageServer.DependencyProvider:
    """The dependency provider of a SystemVerilog project."""
    return SystemVerilogLanguageServer.DependencyProvider(SolidLSPSettings.CustomLSSettings({}), str(tmp_path / "resources"))


def test_a_system_verible_is_used_where_its_version_cannot_be_read(
    stand_ins: StandIns, provider: SystemVerilogLanguageServer.DependencyProvider, caplog: pytest.LogCaptureFixture
) -> None:
    verible = stand_ins.answering("verible-verilog-ls", stderr="verible-verilog-ls: unknown flag --version\n", exit_code=1)
    caplog.set_level("INFO")

    assert provider._get_or_install_core_dependency() == str(verible.path)
    assert f"Using system-installed verible-verilog-ls at {verible.path}" in caplog.text
    assert_started_through_brain_supervise(verible)


def test_a_system_verible_is_used_where_it_cannot_be_started(
    stand_ins: StandIns, provider: SystemVerilogLanguageServer.DependencyProvider, caplog: pytest.LogCaptureFixture
) -> None:
    verible = stand_ins.unrunnable("verible-verilog-ls")
    caplog.set_level("INFO")

    assert provider._get_or_install_core_dependency() == str(verible)
    assert f"Using system-installed verible-verilog-ls at {verible}" in caplog.text
