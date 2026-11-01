"""
:class:`DotNETUtil` starts ``dotnet`` and the .NET install script through brain-supervise, each run against a stand-in on
PATH.
"""

import os
import urllib.request
from pathlib import Path

import pytest

from serena.util.dotnet import DotNETUtil
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


@pytest.fixture
def stand_ins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StandIns:
    """The test's stand-ins, alone on PATH, under a restart root of the test's own."""
    return StandIns(tmp_path / "stand_ins", monkeypatch)


def test_the_installed_versions_are_the_netcore_runtimes_dotnet_lists(stand_ins: StandIns) -> None:
    dotnet = stand_ins.answering(
        "dotnet",
        stdout=(
            "Microsoft.AspNetCore.App 8.0.4 [/usr/local/share/dotnet/shared/Microsoft.AspNetCore.App]\n"
            "Microsoft.NETCore.App 6.0.29 [/usr/local/share/dotnet/shared/Microsoft.NETCore.App]\n"
            "Microsoft.NETCore.App 8.0.4 [/usr/local/share/dotnet/shared/Microsoft.NETCore.App]\n"
        ),
    )

    versions = DotNETUtil("8.0")._determine_installed_versions()

    assert [version.components for version in versions] == [[6, 0, 29], [8, 0, 4]]
    assert_started_through_brain_supervise(dotnet)


def test_the_install_script_installs_the_dotnet_that_is_returned(
    stand_ins: StandIns, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the script as downloaded, and a bash that installs a dotnet into the --install-dir it is given
    monkeypatch.setattr(urllib.request, "urlretrieve", lambda url, path: Path(path).write_text("# dotnet-install.sh\n"))
    bash = stand_ins.add(
        "bash",
        """
        while [ $# -gt 0 ]; do
            if [ "$1" = --install-dir ]; then echo 'installed by the install script' > "$2/dotnet"; fi
            shift
        done
        """,
    )
    base = tmp_path / "base"

    dotnet = Path(DotNETUtil.install_dotnet_with_script("10.0", str(base)))

    assert dotnet.read_text() == "installed by the install script\n"
    assert dotnet.is_relative_to(base)
    assert_started_through_brain_supervise(bash)
