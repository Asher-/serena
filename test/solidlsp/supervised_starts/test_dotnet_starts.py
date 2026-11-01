"""
The .NET family starts ``dotnet`` and ``mono`` through brain-supervise: the runtime probe of :class:`PlatformUtils`, the
FsAutoComplete install and the .NET root lookup of :class:`FSharpLanguageServer`, and the build of the C# structural
bridge, each run against stand-ins on PATH.
"""

import os
from pathlib import Path

import pytest

from solidlsp.language_servers.fsharp_language_server import FSharpLanguageServer
from solidlsp.ls_config import Language, LanguageServerConfig
from solidlsp.ls_utils import DotnetVersion, PlatformUtils
from solidlsp.settings import SolidLSPSettings
from solidlsp.structural.backends import csharp as csharp_backend
from solidlsp.structural.errors import ParseError
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")

_RUNTIMES = (
    "Microsoft.AspNetCore.App 8.0.4 [/usr/local/share/dotnet/shared/Microsoft.AspNetCore.App]\n"
    "Microsoft.NETCore.App 8.0.4 [/usr/local/share/dotnet/shared/Microsoft.NETCore.App]\n"
)
"""What ``dotnet --list-runtimes`` prints where .NET 8 is installed."""


def test_the_dotnet_version_is_the_netcore_runtime_dotnet_lists(stand_ins: StandIns) -> None:
    dotnet = stand_ins.answering("dotnet", stdout=_RUNTIMES)

    assert PlatformUtils.get_dotnet_version() == DotnetVersion.V8
    assert_started_through_brain_supervise(dotnet)


def test_mono_is_the_dotnet_version_where_dotnet_fails_and_mono_runs(stand_ins: StandIns) -> None:
    dotnet = stand_ins.answering("dotnet", stderr="dotnet: no runtimes\n", exit_code=1)
    mono = stand_ins.answering("mono", stdout="Mono JIT compiler version 6.12.0.199\n")

    assert PlatformUtils.get_dotnet_version() == DotnetVersion.VMONO
    assert_started_through_brain_supervise(dotnet, mono)


def test_fsautocomplete_is_installed_by_dotnet_tool_install_and_launched_from_where_it_was_installed(
    stand_ins: StandIns, tmp_path: Path
) -> None:
    # a dotnet that lists .NET 8 and installs a tool into the --tool-path it is given
    dotnet = stand_ins.add(
        "dotnet",
        f"""
        case "$1" in
            --list-runtimes) printf '%s' '{_RUNTIMES}' ;;
            tool)
                while [ $# -gt 0 ]; do
                    if [ "$1" = --tool-path ]; then echo 'installed by dotnet tool install' > "$2/fsautocomplete"; fi
                    shift
                done ;;
        esac
        """,
    )
    settings = SolidLSPSettings(solidlsp_dir=str(tmp_path / "solidlsp"))

    command = FSharpLanguageServer._setup_runtime_dependencies(LanguageServerConfig(code_language=Language.FSHARP), settings)

    assert Path(command.split()[0]).read_text() == "installed by dotnet tool install\n"
    assert_started_through_brain_supervise(dotnet)


def test_the_dotnet_root_is_two_levels_above_the_base_path_dotnet_info_prints(stand_ins: StandIns) -> None:
    dotnet = stand_ins.answering(
        "dotnet", stdout=".NET SDK:\n Version:   8.0.100\n\nRuntime Environment:\n Base Path:   /opt/dotnet/sdk/8.0.100/\n"
    )
    server = FSharpLanguageServer.__new__(FSharpLanguageServer)

    assert server._get_dotnet_root() == "/opt/dotnet"
    assert_started_through_brain_supervise(dotnet)


def test_a_failed_csharp_bridge_build_raises_with_what_dotnet_build_wrote(
    stand_ins: StandIns, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(csharp_backend, "_BRIDGE_PACKAGE_DIR", tmp_path / "csharp_bridge")
    dotnet = stand_ins.answering("dotnet", stdout="Program.cs(3,1): error CS1002: ; expected\n", exit_code=1)
    monkeypatch.setenv("SERENA_DOTNET", str(dotnet.path))

    with pytest.raises(ParseError) as caught:
        csharp_backend._build_bridge_if_needed()
    assert "Program.cs(3,1): error CS1002: ; expected" in caught.value.detail
    assert_started_through_brain_supervise(dotnet)
