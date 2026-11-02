"""
The OCaml family's runs of ``opam`` through brain-supervise, where ``opam`` fails, is not installed, cannot be started or
outlasts its timeout: the version probes, the install check and the index build of :class:`OcamlLanguageServer`, each
run against stand-ins on PATH. The 120 seconds the index build is given are not waited out: the run that would time out
is replaced by one that does.
"""

import os
from pathlib import Path

import pytest

from solidlsp.language_servers.ocaml_lsp_server import OcamlLanguageServer
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise, time_out_runs

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """The OCaml project opam runs in."""
    root = tmp_path / "project"
    root.mkdir()
    return root


def test_the_ocaml_version_cannot_be_detected_where_opam_fails(stand_ins: StandIns, project: Path) -> None:
    opam = stand_ins.answering("opam", stderr="opam: no switch is currently set\n", exit_code=2)

    with pytest.raises(RuntimeError) as caught:
        OcamlLanguageServer._detect_ocaml_version(str(project))
    assert "Failed to detect OCaml version: opam: no switch is currently set" in str(caught.value)
    assert_started_through_brain_supervise(opam)


def test_the_ocaml_version_cannot_be_detected_where_opam_is_not_installed(stand_ins: StandIns, project: Path) -> None:
    with pytest.raises(RuntimeError) as caught:
        OcamlLanguageServer._detect_ocaml_version(str(project))
    assert "OCaml not found. Please install OCaml via opam" in str(caught.value)


def test_the_lsp_version_cannot_be_detected_where_opam_fails(stand_ins: StandIns, project: Path) -> None:
    opam = stand_ins.answering("opam", stderr="opam: no switch is currently set\n", exit_code=2)

    with pytest.raises(RuntimeError) as caught:
        OcamlLanguageServer._detect_lsp_version(str(project))
    assert "Failed to detect ocaml-lsp-server version: opam: no switch is currently set" in str(caught.value)
    assert_started_through_brain_supervise(opam)


def test_the_lsp_version_cannot_be_detected_where_opam_is_not_installed(stand_ins: StandIns, project: Path) -> None:
    with pytest.raises(RuntimeError) as caught:
        OcamlLanguageServer._detect_lsp_version(str(project))
    assert "opam not found. Please install opam" in str(caught.value)


@pytest.mark.parametrize("listed", ["", "# No matches found for ocaml-lsp-server\n"])
def test_ocaml_lsp_server_is_not_installed_where_opam_list_does_not_list_it(stand_ins: StandIns, project: Path, listed: str) -> None:
    opam = stand_ins.answering("opam", stdout=listed)

    with pytest.raises(RuntimeError) as caught:
        OcamlLanguageServer._ensure_ocaml_lsp_installed(str(project))
    assert "ocaml-lsp-server is not installed" in str(caught.value)
    assert_started_through_brain_supervise(opam)


def test_the_ocaml_lsp_executable_cannot_be_found_where_opam_exec_fails(stand_ins: StandIns, project: Path) -> None:
    # an opam that lists ocaml-lsp-server as installed and fails to find its executable
    opam = stand_ins.add(
        "opam",
        """
        case "$1" in
            list) echo 'ocaml-lsp-server  1.23.0  LSP Server for OCaml' ;;
            exec) echo 'opam: command not found: which' >&2; exit 5 ;;
        esac
        """,
    )

    with pytest.raises(RuntimeError) as caught:
        OcamlLanguageServer._ensure_ocaml_lsp_installed(str(project))
    assert "Failed to find ocaml-lsp-server executable." in str(caught.value)
    assert "Return code: 5" in str(caught.value)
    assert "Stderr: opam: command not found: which" in str(caught.value)
    assert_started_through_brain_supervise(opam)


def test_the_index_is_not_built_where_opam_is_not_installed(stand_ins: StandIns, project: Path) -> None:
    assert OcamlLanguageServer._build_ocaml_index_static(str(project)) is False


def test_the_index_is_not_built_where_opam_cannot_be_started(stand_ins: StandIns, project: Path, caplog: pytest.LogCaptureFixture) -> None:
    stand_ins.unrunnable("opam")

    assert OcamlLanguageServer._build_ocaml_index_static(str(project)) is False
    assert "Error building OCaml index: supervise: start" in caplog.text


def test_the_index_is_not_built_where_dune_build_times_out(stand_ins: StandIns, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    timed_out = time_out_runs(monkeypatch)

    assert OcamlLanguageServer._build_ocaml_index_static(str(project)) is False
    assert [(run.args, run.timeout) for run in timed_out] == [(["opam", "exec", "--", "dune", "build", "@ocaml-index"], 120)]
