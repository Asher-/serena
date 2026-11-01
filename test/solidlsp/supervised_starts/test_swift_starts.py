"""
The Swift family starts ``xcrun``, ``sourcekit-lsp`` and ``swift`` through brain-supervise: the path and version probes
of :class:`SourceKitLSP` and the build of the Swift structural bridge, each run against stand-ins on PATH.
"""

import os
import shlex
from pathlib import Path

import pytest

from solidlsp.language_servers.sourcekit_lsp import SourceKitLSP
from solidlsp.structural.backends import swift as swift_backend
from solidlsp.structural.errors import ParseError
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


def test_sourcekit_lsp_is_the_one_xcrun_finds_without_the_xcode_developer_dir(stand_ins: StandIns, tmp_path: Path) -> None:
    # an xcrun that finds nothing under Xcode's developer directory and finds the toolchain's sourcekit-lsp otherwise
    sourcekit_lsp = tmp_path / "toolchain" / "usr" / "bin" / "sourcekit-lsp"
    sourcekit_lsp.parent.mkdir(parents=True)
    sourcekit_lsp.write_text("#!/bin/sh\n")
    xcrun = stand_ins.add(
        "xcrun",
        f'if [ -n "$DEVELOPER_DIR" ]; then echo "xcrun: error: unable to find utility" >&2; exit 72; fi\necho {shlex.quote(str(sourcekit_lsp))}',
    )

    assert SourceKitLSP._resolve_sourcekit_lsp_path() == str(sourcekit_lsp)
    assert_started_through_brain_supervise(xcrun)


def test_the_sourcekit_lsp_version_is_what_sourcekit_lsp_h_prints(stand_ins: StandIns) -> None:
    sourcekit_lsp = stand_ins.answering(
        "sourcekit-lsp", stdout="OVERVIEW: Language Server Protocol implementation for Swift and C-based languages\n"
    )

    assert SourceKitLSP._get_sourcekit_lsp_version() == "OVERVIEW: Language Server Protocol implementation for Swift and C-based languages"
    assert_started_through_brain_supervise(sourcekit_lsp)


def test_a_failed_bridge_build_raises_with_what_swift_build_wrote(
    stand_ins: StandIns, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(swift_backend, "_BRIDGE_PACKAGE_DIR", tmp_path / "swift_bridge")
    swift = stand_ins.answering("swift", stderr="error: no such module 'SwiftSyntax'\n", exit_code=1)

    with pytest.raises(ParseError) as caught:
        swift_backend._build_bridge_if_needed()
    assert "error: no such module 'SwiftSyntax'" in caught.value.detail
    assert_started_through_brain_supervise(swift)
