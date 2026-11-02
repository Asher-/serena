"""
The .NET family's runs of ``dotnet`` and ``mono`` through brain-supervise, where a program fails or is not installed, and
where the bridge build succeeds: the runtime probe of :class:`PlatformUtils`, the FsAutoComplete install and the .NET
root lookup of :class:`FSharpLanguageServer`, and the build of the C# structural bridge, each run against stand-ins on
PATH.
"""

import os
import shlex
from pathlib import Path

import pytest

from solidlsp.language_servers.fsharp_language_server import FSharpLanguageServer
from solidlsp.ls_config import LanguageServerId, LanguageServerConfig
from solidlsp.ls_exceptions import SolidLSPException
from solidlsp.ls_utils import PlatformUtils
from solidlsp.settings import SolidLSPSettings
from solidlsp.structural.backends import csharp as csharp_backend
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")

_RUNTIMES = "Microsoft.NETCore.App 8.0.4 [/usr/local/share/dotnet/shared/Microsoft.NETCore.App]\n"
"""What ``dotnet --list-runtimes`` prints where .NET 8 is installed."""


def test_no_dotnet_version_is_found_where_dotnet_fails_and_mono_is_not_installed(stand_ins: StandIns) -> None:
    dotnet = stand_ins.answering("dotnet", stderr="dotnet: no runtimes\n", exit_code=1)

    with pytest.raises(SolidLSPException) as caught:
        PlatformUtils.get_dotnet_version()
    assert "dotnet or mono not found on the system" in str(caught.value)
    assert_started_through_brain_supervise(dotnet)


def test_no_dotnet_version_is_found_where_dotnet_and_mono_both_fail(stand_ins: StandIns) -> None:
    dotnet = stand_ins.answering("dotnet", stderr="dotnet: no runtimes\n", exit_code=1)
    mono = stand_ins.answering("mono", stderr="mono: cannot load the runtime\n", exit_code=1)

    with pytest.raises(SolidLSPException) as caught:
        PlatformUtils.get_dotnet_version()
    assert "dotnet or mono not found on the system" in str(caught.value)
    assert_started_through_brain_supervise(dotnet, mono)


def test_no_dotnet_version_is_found_where_neither_dotnet_nor_mono_is_installed(stand_ins: StandIns) -> None:
    with pytest.raises(SolidLSPException) as caught:
        PlatformUtils.get_dotnet_version()
    assert "dotnet or mono not found on the system" in str(caught.value)


def test_fsautocomplete_setup_fails_with_what_a_failing_dotnet_tool_install_wrote(stand_ins: StandIns, tmp_path: Path) -> None:
    # a dotnet that lists .NET 8 and fails to install a tool
    dotnet = stand_ins.add(
        "dotnet",
        f"""
        case "$1" in
            --list-runtimes) printf '%s' '{_RUNTIMES}' ;;
            tool) echo 'error NU1101: Unable to find package fsautocomplete' >&2; exit 1 ;;
        esac
        """,
    )
    settings = SolidLSPSettings(solidlsp_dir=str(tmp_path / "solidlsp"))

    with pytest.raises(RuntimeError) as caught:
        FSharpLanguageServer._setup_runtime_dependencies(LanguageServerConfig(code_language=LanguageServerId.FSHARP), settings)
    assert "Failed to install FsAutoComplete: error NU1101: Unable to find package fsautocomplete" in str(caught.value)
    assert_started_through_brain_supervise(dotnet)


def test_the_dotnet_root_is_the_directory_of_dotnet_where_dotnet_info_fails(stand_ins: StandIns) -> None:
    dotnet = stand_ins.answering("dotnet", stderr="dotnet: no SDK found\n", exit_code=1)
    server = FSharpLanguageServer.__new__(FSharpLanguageServer)

    assert server._get_dotnet_root() == str(stand_ins.bin)
    assert_started_through_brain_supervise(dotnet)


def test_the_csharp_bridge_is_the_dll_dotnet_build_wrote_in_the_package_directory(
    stand_ins: StandIns, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(csharp_backend, "_BRIDGE_PACKAGE_DIR", tmp_path / "csharp_bridge")
    # a dotnet that writes the dll under the directory it runs in
    relative = csharp_backend._BRIDGE_DLL_RELATIVE
    dotnet = stand_ins.add("dotnet", f"/bin/mkdir -p {shlex.quote(str(relative.parent))}; /usr/bin/touch {shlex.quote(str(relative))}")
    monkeypatch.setenv("SERENA_DOTNET", str(dotnet.path))

    dll = csharp_backend._build_bridge_if_needed()

    assert dll == tmp_path / "csharp_bridge" / relative
    assert dll.exists()
    assert_started_through_brain_supervise(dotnet)
