"""
The Rust family's runs of ``rustup``, ``rust-analyzer`` and ``cargo`` through brain-supervise, where a program fails, is
not installed or cannot be started, and where the bridge build succeeds: the probes and the rustup install of
:class:`RustAnalyzer.DependencyProvider` and the build of the Rust structural bridge, each run against stand-ins on PATH.
"""

import os
import shlex
from pathlib import Path

import pytest

from solidlsp.language_servers.rust_analyzer import RustAnalyzer
from solidlsp.structural.backends import rust as rust_backend
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


def test_there_is_no_rustup_version_where_rustup_fails(stand_ins: StandIns) -> None:
    rustup = stand_ins.answering("rustup", stderr="error: no default toolchain configured\n", exit_code=1)

    assert RustAnalyzer.DependencyProvider._get_rustup_version() is None
    assert_started_through_brain_supervise(rustup)


def test_there_is_no_rustup_version_where_rustup_is_not_installed(stand_ins: StandIns) -> None:
    assert RustAnalyzer.DependencyProvider._get_rustup_version() is None


def test_rustup_has_no_rust_analyzer_where_rustup_is_not_installed(stand_ins: StandIns) -> None:
    assert RustAnalyzer.DependencyProvider._get_rust_analyzer_via_rustup() is None


def test_a_rust_analyzer_that_is_not_installed_is_not_functional(stand_ins: StandIns, tmp_path: Path) -> None:
    assert RustAnalyzer.DependencyProvider._is_rust_analyzer_functional(str(tmp_path / "rust-analyzer")) is False


def test_a_rust_analyzer_that_cannot_be_started_is_not_functional(stand_ins: StandIns) -> None:
    rust_analyzer = stand_ins.unrunnable("rust-analyzer")

    assert RustAnalyzer.DependencyProvider._is_rust_analyzer_functional(str(rust_analyzer)) is False


def test_rust_analyzer_is_the_functional_one_on_path_where_rustup_cannot_add_it(stand_ins: StandIns) -> None:
    # a rustup that has no rust-analyzer component and cannot add one
    rustup = stand_ins.add(
        "rustup",
        """
        case "$1" in
            --version) echo 'rustup 1.27.1 (54dd3d00f 2024-04-24)' ;;
            *) echo 'error: component rust-analyzer is unavailable for download' >&2; exit 1 ;;
        esac
        """,
    )
    rust_analyzer = stand_ins.answering("rust-analyzer", stdout="rust-analyzer 1.79.0 (129f3b99 2024-06-10)\n")

    assert RustAnalyzer.DependencyProvider._ensure_rust_analyzer_installed() == str(rust_analyzer.path)
    assert_started_through_brain_supervise(rustup, rust_analyzer)


def test_the_bridge_is_the_binary_cargo_build_wrote_in_the_package_directory(
    stand_ins: StandIns, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rust_backend, "_BRIDGE_PACKAGE_DIR", tmp_path / "rust_bridge")
    # a cargo that writes the binary under the directory it runs in
    relative = rust_backend._BRIDGE_BINARY_RELATIVE
    cargo = stand_ins.add("cargo", f"/bin/mkdir -p {shlex.quote(str(relative.parent))}; /usr/bin/touch {shlex.quote(str(relative))}")

    binary = rust_backend._build_bridge_if_needed()

    assert binary == tmp_path / "rust_bridge" / relative
    assert binary.exists()
    assert_started_through_brain_supervise(cargo)
