"""
:class:`DotNETUtil` starts ``dotnet`` and the .NET install script through brain-supervise: the runtime list, and the
install script that Microsoft serves, each run against stand-ins on PATH. The script is not downloaded: what the download
fetches is a script of the test's, which the stand-in ``bash`` runs.
"""

import os
import urllib.request
from pathlib import Path

import pytest

from serena.util.dotnet import DotNETUtil
from solidlsp.ls_exceptions import SolidLSPException
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")

_INSTALL_SCRIPT = """\
arguments="$*"
while [ $# -gt 0 ]; do
    if [ "$1" = --install-dir ]; then install_dir=$2; fi
    shift
done
echo "$arguments" > "$install_dir/arguments"
echo 'installed by the install script' > "$install_dir/dotnet"
"""
"""An install script: it writes the arguments it was given and a ``dotnet`` into its install directory."""

_DOWNLOAD_URL = "https://dot.net/v1/dotnet-install.sh"


@pytest.fixture
def downloaded_urls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The urls the install script was downloaded from, where downloading writes :data:`_INSTALL_SCRIPT` to the path asked."""
    urls: list[str] = []

    def urlretrieve(url: str, filename: str | os.PathLike) -> None:
        urls.append(url)
        Path(filename).write_text(_INSTALL_SCRIPT)

    monkeypatch.setattr(urllib.request, "urlretrieve", urlretrieve)
    return urls


@pytest.fixture
def base_path(tmp_path: Path) -> Path:
    """The directory the runtime is installed under."""
    path = tmp_path / "runtimes"
    path.mkdir()
    return path


def test_no_installed_version_is_known_where_dotnet_fails_to_list_its_runtimes(
    stand_ins: StandIns, caplog: pytest.LogCaptureFixture
) -> None:
    dotnet = stand_ins.answering("dotnet", stderr="dotnet: no runtimes\n", exit_code=1)

    with pytest.raises(SolidLSPException) as caught:
        DotNETUtil("8.0").get_dotnet_path_or_raise()
    assert "(installed versions: [])" in str(caught.value)
    assert "Failed to run 'dotnet --list-runtimes'" in caplog.text
    assert_started_through_brain_supervise(dotnet)


def test_the_runtime_is_the_dotnet_the_install_script_wrote_to_the_install_directory(
    stand_ins: StandIns, base_path: Path, downloaded_urls: list[str]
) -> None:
    # a bash that runs the install script it is given, as sh
    bash = stand_ins.add("bash", '/bin/sh "$@"')

    dotnet = DotNETUtil.install_dotnet_with_script("10.0", str(base_path))

    install_dir = base_path / "dotnet-runtime-10.0"
    assert dotnet == str(install_dir / "dotnet")
    assert Path(dotnet).read_text() == "installed by the install script\n"
    assert (install_dir / "arguments").read_text() == f"--version 10.0 --install-dir {install_dir} --runtime dotnet --no-path\n"
    assert downloaded_urls == [_DOWNLOAD_URL]
    assert_started_through_brain_supervise(bash)


def test_the_install_fails_with_what_a_failing_install_script_wrote(
    stand_ins: StandIns, base_path: Path, downloaded_urls: list[str]
) -> None:
    bash = stand_ins.answering("bash", stderr="dotnet-install: Error: Could not find .NET runtime 10.0\n", exit_code=1)

    with pytest.raises(SolidLSPException) as caught:
        DotNETUtil.install_dotnet_with_script("10.0", str(base_path))
    assert "Failed to install .NET 10.0 runtime using install script: dotnet-install: Error: Could not find .NET runtime 10.0" in str(
        caught.value
    )
    assert_started_through_brain_supervise(bash)


def test_the_install_fails_where_bash_is_not_installed(stand_ins: StandIns, base_path: Path, downloaded_urls: list[str]) -> None:
    with pytest.raises(SolidLSPException) as caught:
        DotNETUtil.install_dotnet_with_script("10.0", str(base_path))
    assert "Failed to install .NET 10.0 runtime: " in str(caught.value)
    assert isinstance(caught.value.__cause__, FileNotFoundError)


def test_the_install_fails_where_the_install_script_wrote_no_dotnet(
    stand_ins: StandIns, base_path: Path, downloaded_urls: list[str]
) -> None:
    bash = stand_ins.answering("bash")

    with pytest.raises(SolidLSPException) as caught:
        DotNETUtil.install_dotnet_with_script("10.0", str(base_path))
    assert (
        f"Failed to install .NET 10.0 runtime: dotnet executable not found at {base_path / 'dotnet-runtime-10.0' / 'dotnet'} after"
        in str(caught.value)
    )
    assert_started_through_brain_supervise(bash)
