"""
The Rust family starts ``rustup``, ``rust-analyzer`` and ``cargo`` through brain-supervise: the probes and the rustup
install of :class:`RustAnalyzer.DependencyProvider`, and the build of the Rust structural bridge, each run against
stand-ins on PATH.
"""

import os
import shlex
from pathlib import Path

import pytest

from solidlsp.language_servers.rust_analyzer import RustAnalyzer
from solidlsp.structural.backends import rust as rust_backend
from solidlsp.structural.errors import ParseError
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")

_TOOLCHAIN_RUST_ANALYZER = "/home/user/.rustup/toolchains/stable-aarch64-apple-darwin/bin/rust-analyzer"


def test_the_rustup_version_is_what_rustup_version_prints(stand_ins: StandIns) -> None:
    rustup = stand_ins.answering("rustup", stdout="rustup 1.27.1 (54dd3d00f 2024-04-24)\n")

    assert RustAnalyzer.DependencyProvider._get_rustup_version() == "rustup 1.27.1 (54dd3d00f 2024-04-24)"
    assert_started_through_brain_supervise(rustup)


def test_the_rustup_rust_analyzer_is_the_path_rustup_which_prints(stand_ins: StandIns) -> None:
    rustup = stand_ins.answering("rustup", stdout=_TOOLCHAIN_RUST_ANALYZER + "\n")

    assert RustAnalyzer.DependencyProvider._get_rust_analyzer_via_rustup() == _TOOLCHAIN_RUST_ANALYZER
    assert_started_through_brain_supervise(rustup)


@pytest.mark.parametrize(("exit_code", "functional"), [(0, True), (1, False)])
def test_a_rust_analyzer_is_functional_where_its_version_runs(stand_ins: StandIns, exit_code: int, functional: bool) -> None:
    rust_analyzer = stand_ins.answering("rust-analyzer", stdout="rust-analyzer 1.79.0 (129f3b99 2024-06-10)\n", exit_code=exit_code)

    assert RustAnalyzer.DependencyProvider._is_rust_analyzer_functional(str(rust_analyzer.path)) is functional
    assert_started_through_brain_supervise(rust_analyzer)


def test_rust_analyzer_is_the_one_rustup_installs_when_rustup_has_none(stand_ins: StandIns, tmp_path: Path) -> None:
    # a rustup with no rust-analyzer component until one is added
    added = shlex.quote(str(tmp_path / "component-added"))
    rustup = stand_ins.add(
        "rustup",
        f"""
        case "$1" in
            --version) echo 'rustup 1.27.1 (54dd3d00f 2024-04-24)' ;;
            which) if [ -e {added} ]; then echo '{_TOOLCHAIN_RUST_ANALYZER}'; else exit 1; fi ;;
            component) /usr/bin/touch {added} ;;
        esac
        """,
    )

    assert RustAnalyzer.DependencyProvider._ensure_rust_analyzer_installed() == _TOOLCHAIN_RUST_ANALYZER
    assert_started_through_brain_supervise(rustup)


def test_a_failed_bridge_build_raises_with_what_cargo_build_wrote(
    stand_ins: StandIns, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rust_backend, "_BRIDGE_PACKAGE_DIR", tmp_path / "rust_bridge")
    cargo = stand_ins.answering("cargo", stderr="error[E0425]: cannot find value `x` in this scope\n", exit_code=101)

    with pytest.raises(ParseError) as caught:
        rust_backend._build_bridge_if_needed()
    assert "error[E0425]: cannot find value `x` in this scope" in caught.value.detail
    assert_started_through_brain_supervise(cargo)
