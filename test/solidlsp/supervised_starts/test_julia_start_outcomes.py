"""
The Julia family's runs of ``julia`` through brain-supervise, where LanguageServer.jl is installed already and where a
check or an install outlasts its timeout: the check and install of :class:`JuliaLanguageServer`, run against a stand-in
on PATH. A timeout of 10 or 300 seconds is not waited out: the run that would time out is replaced by one that does.
"""

import os
import shlex
from pathlib import Path

import pytest

from solidlsp.language_servers.julia_server import JuliaLanguageServer
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise, time_out_runs

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


def test_language_server_jl_is_not_installed_where_julia_loads_it(stand_ins: StandIns, tmp_path: Path) -> None:
    # a julia that loads LanguageServer.jl and marks an install of anything
    installed = tmp_path / "language-server-installed"
    julia = stand_ins.add(
        "julia",
        f"""
        case "$2" in
            "using LanguageServer") ;;
            *) /usr/bin/touch {shlex.quote(str(installed))} ;;
        esac
        """,
    )

    assert JuliaLanguageServer._setup_runtime_dependency() == str(julia.path)
    assert not installed.exists()
    assert_started_through_brain_supervise(julia)


def test_language_server_jl_is_installed_where_the_check_that_loads_it_times_out(
    stand_ins: StandIns, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a julia that marks an install; only the check that loads LanguageServer.jl times out
    installed = tmp_path / "language-server-installed"
    julia = stand_ins.add("julia", f"/usr/bin/touch {shlex.quote(str(installed))}")
    timed_out = time_out_runs(monkeypatch, matching=lambda args: args[-1] == "using LanguageServer")

    assert JuliaLanguageServer._setup_runtime_dependency() == str(julia.path)
    assert [(run.args, run.timeout) for run in timed_out] == [([str(julia.path), "-e", "using LanguageServer"], 10)]
    assert installed.exists()
    assert_started_through_brain_supervise(julia)


def test_an_install_that_times_out_raises_telling_how_to_install_by_hand(monkeypatch: pytest.MonkeyPatch) -> None:
    julia = "/opt/julia/bin/julia"
    timed_out = time_out_runs(monkeypatch)

    with pytest.raises(RuntimeError) as caught:
        JuliaLanguageServer._install_language_server(julia)
    assert "LanguageServer.jl installation timed out. Please install manually" in str(caught.value)
    assert [(run.args, run.timeout) for run in timed_out] == [([julia, "-e", 'using Pkg; Pkg.add("LanguageServer")'], 300)]
