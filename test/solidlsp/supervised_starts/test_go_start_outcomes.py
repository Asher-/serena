"""
The Go family's runs of ``go`` and ``gopls`` through brain-supervise, where the program fails or is not installed, and
where the bridge build succeeds: the toolchain probes of :class:`Gopls` and the build of the Go structural bridge, each
run against stand-ins on PATH.
"""

import os
from pathlib import Path

import pytest

from solidlsp.language_servers.gopls import Gopls
from solidlsp.structural.backends import go as go_backend
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


def test_there_is_no_go_version_where_go_fails(stand_ins: StandIns) -> None:
    go = stand_ins.answering("go", stderr="go: cannot find GOROOT directory\n", exit_code=2)

    assert Gopls._get_go_version() is None
    assert_started_through_brain_supervise(go)


def test_there_is_no_go_version_where_go_is_not_installed(stand_ins: StandIns) -> None:
    assert Gopls._get_go_version() is None


def test_there_is_no_gopls_version_where_gopls_fails(stand_ins: StandIns) -> None:
    gopls = stand_ins.answering("gopls", stderr="gopls: no Go toolchain\n", exit_code=1)

    assert Gopls._get_gopls_version() is None
    assert_started_through_brain_supervise(gopls)


def test_there_is_no_gopls_version_where_gopls_is_not_installed(stand_ins: StandIns) -> None:
    assert Gopls._get_gopls_version() is None


def test_the_bridge_is_the_binary_go_build_wrote_to_the_path_it_was_given(
    stand_ins: StandIns, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(go_backend, "_BRIDGE_PACKAGE_DIR", tmp_path / "go_bridge")
    # a go that writes the binary to the path after -o
    go = stand_ins.add("go", 'echo "built in $(/bin/pwd -P)" > "$3"')

    binary = go_backend._build_bridge_if_needed()

    assert binary == tmp_path / "go_bridge" / go_backend._BRIDGE_BINARY_RELATIVE
    assert binary.read_text() == f"built in {(tmp_path / 'go_bridge').resolve()}\n"
    assert_started_through_brain_supervise(go)
