"""
OmniSharp is started through brain-supervise and handed no pid of serena's to watch: given ``--hostPID``, OmniSharp ends
when the process it names ends, so a language server serena started would end with serena instead of outliving it
(bug://serena/language_server/initialize/process_id/client/watch). The OmniSharp run here is a stand-in that logs the
arguments it was started with.
"""

import os
import time
from pathlib import Path

import pytest

from solidlsp.language_servers.omnisharp import OmniSharp
from solidlsp.ls_config import LanguageServerConfig, LanguageServerId
from solidlsp.ls_exceptions import SolidLSPException
from solidlsp.settings import SolidLSPSettings
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")

_DEADLINE_SECONDS = 60.0
"""How long a broken implementation may hang the test before it fails."""


def test_omnisharp_is_started_through_brain_supervise_and_handed_no_pid_to_watch(
    stand_ins: StandIns, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a repository holding a solution, and an OmniSharp that logs its arguments, one a line, and then runs on
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "App.sln").write_text("")
    arguments = tmp_path / "arguments"
    omnisharp = stand_ins.add(
        "omnisharp", f"printf '%s\\n' \"$@\" > {arguments}.part\n/bin/mv {arguments}.part {arguments}\nexec /bin/sleep 300"
    )
    plugin = tmp_path / "OmniSharp.Razor.dll"
    monkeypatch.setattr(
        OmniSharp, "_setup_runtime_dependencies", classmethod(lambda cls, config, settings: (str(omnisharp.path), str(plugin)))
    )
    settings = SolidLSPSettings(solidlsp_dir=str(tmp_path / "solidlsp"), project_data_path=str(tmp_path / "project_data"))
    language_server = OmniSharp(LanguageServerConfig(ls_id=LanguageServerId.CSHARP_OMNISHARP), str(repository), settings)

    # the language server's process, started as serena starts it, and stopped once it has logged its arguments
    language_server.server.start()
    try:
        deadline = time.monotonic() + _DEADLINE_SECONDS
        while not arguments.exists():
            assert time.monotonic() < deadline, "OmniSharp never ran"
            time.sleep(0.01)
    finally:
        language_server.server.stop()

    started_with = arguments.read_text().splitlines()
    assert "-lsp" in started_with
    assert "--hostPID" not in started_with
    assert str(os.getpid()) not in started_with
    assert_started_through_brain_supervise(omnisharp)


def test_omnisharp_refuses_a_repository_holding_no_solution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.setattr(OmniSharp, "_setup_runtime_dependencies", classmethod(lambda cls, config, settings: ("omnisharp", "plugin.dll")))
    settings = SolidLSPSettings(solidlsp_dir=str(tmp_path / "solidlsp"), project_data_path=str(tmp_path / "project_data"))

    with pytest.raises(SolidLSPException, match="No SLN file found"):
        OmniSharp(LanguageServerConfig(ls_id=LanguageServerId.CSHARP_OMNISHARP), str(repository), settings)
