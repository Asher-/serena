"""
The Swift family's runs of ``xcrun``, ``sourcekit-lsp`` and ``swift`` through brain-supervise, where a program fails or is
not installed, and where the bridge build succeeds: the path and version probes of :class:`SourceKitLSP` and the build
of the Swift structural bridge, each run against stand-ins on PATH.
"""

import os
import shlex
from pathlib import Path

import pytest

from solidlsp.language_servers.sourcekit_lsp import SourceKitLSP
from solidlsp.structural.backends import swift as swift_backend
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


def test_sourcekit_lsp_is_the_one_on_path_where_xcrun_is_not_installed(stand_ins: StandIns) -> None:
    assert SourceKitLSP._resolve_sourcekit_lsp_path() == "sourcekit-lsp"


def test_the_sourcekit_lsp_version_cannot_be_read_where_sourcekit_lsp_fails(stand_ins: StandIns) -> None:
    sourcekit_lsp = stand_ins.answering("sourcekit-lsp", stderr="sourcekit-lsp: error: no toolchain\n", exit_code=1)

    with pytest.raises(RuntimeError) as caught:
        SourceKitLSP._get_sourcekit_lsp_version()
    assert "Could not find sourcekit-lsp" in str(caught.value)
    assert "`sourcekit-lsp -h` resulted in:" in str(caught.value.__cause__)
    assert "returncode=1" in str(caught.value.__cause__)
    assert "sourcekit-lsp: error: no toolchain" in str(caught.value.__cause__)
    assert_started_through_brain_supervise(sourcekit_lsp)


def test_the_sourcekit_lsp_version_cannot_be_read_where_sourcekit_lsp_is_not_installed(stand_ins: StandIns) -> None:
    with pytest.raises(RuntimeError) as caught:
        SourceKitLSP._get_sourcekit_lsp_version()
    assert "Could not find sourcekit-lsp" in str(caught.value)
    assert isinstance(caught.value.__cause__, FileNotFoundError)


def test_the_swift_bridge_is_the_binary_swift_build_wrote_in_the_package_directory(
    stand_ins: StandIns, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(swift_backend, "_BRIDGE_PACKAGE_DIR", tmp_path / "swift_bridge")
    # a swift that writes the binary under the directory it runs in
    relative = swift_backend._BRIDGE_BINARY_RELATIVE
    swift = stand_ins.add("swift", f"/bin/mkdir -p {shlex.quote(str(relative.parent))}; /usr/bin/touch {shlex.quote(str(relative))}")

    binary = swift_backend._build_bridge_if_needed()

    assert binary == tmp_path / "swift_bridge" / relative
    assert binary.exists()
    assert_started_through_brain_supervise(swift)
