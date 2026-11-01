"""
The Go family starts ``go`` and ``gopls`` through brain-supervise: the toolchain probes of :class:`Gopls` and the build of
the Go structural bridge, each run against stand-ins on PATH.
"""

import os
from pathlib import Path

import pytest

from solidlsp.language_servers.gopls import Gopls
from solidlsp.structural.backends import go as go_backend
from solidlsp.structural.errors import ParseError
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


def test_the_go_version_is_what_go_version_prints(stand_ins: StandIns) -> None:
    go = stand_ins.answering("go", stdout="go version go1.99.1 darwin/arm64\n")

    assert Gopls._get_go_version() == "go version go1.99.1 darwin/arm64"
    assert_started_through_brain_supervise(go)


def test_the_gopls_version_is_what_gopls_version_prints(stand_ins: StandIns) -> None:
    gopls = stand_ins.answering("gopls", stdout="golang.org/x/tools/gopls v0.99.0\n")

    assert Gopls._get_gopls_version() == "golang.org/x/tools/gopls v0.99.0"
    assert_started_through_brain_supervise(gopls)


def test_a_failed_bridge_build_raises_with_what_go_build_wrote(
    stand_ins: StandIns, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(go_backend, "_BRIDGE_PACKAGE_DIR", tmp_path / "go_bridge")
    go = stand_ins.answering("go", stderr="./main.go:1:1: expected 'package', found 'EOF'\n", exit_code=1)

    with pytest.raises(ParseError) as caught:
        go_backend._build_bridge_if_needed()
    assert "./main.go:1:1: expected 'package', found 'EOF'" in caught.value.detail
    assert_started_through_brain_supervise(go)
