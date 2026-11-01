"""
The Julia family starts ``julia`` through brain-supervise: the LanguageServer.jl check and install of
:class:`JuliaLanguageServer`, each run against a stand-in on PATH.
"""

import os
import shlex
from pathlib import Path

import pytest

from solidlsp.language_servers.julia_server import JuliaLanguageServer
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


def test_language_server_jl_is_installed_where_julia_cannot_load_it(stand_ins: StandIns, tmp_path: Path) -> None:
    # a julia without LanguageServer.jl, which marks an install of it
    installed = shlex.quote(str(tmp_path / "language-server-installed"))
    julia = stand_ins.add(
        "julia",
        f"""
        case "$2" in
            "using LanguageServer") echo 'ERROR: ArgumentError: Package LanguageServer not found' >&2; exit 1 ;;
            *) /usr/bin/touch {installed} ;;
        esac
        """,
    )

    assert JuliaLanguageServer._setup_runtime_dependency() == str(julia.path)
    assert (tmp_path / "language-server-installed").exists()
    assert_started_through_brain_supervise(julia)


def test_a_failed_install_raises_with_what_julia_wrote(stand_ins: StandIns) -> None:
    julia = stand_ins.answering("julia", stderr="ERROR: could not download the General registry\n", exit_code=1)

    with pytest.raises(RuntimeError) as caught:
        JuliaLanguageServer._install_language_server(str(julia.path))
    assert "ERROR: could not download the General registry" in str(caught.value)
    assert_started_through_brain_supervise(julia)
