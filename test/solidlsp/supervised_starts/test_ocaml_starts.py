"""
The OCaml family starts ``opam`` through brain-supervise: the version probes, the install check and the index build of
:class:`OcamlLanguageServer`, each run against a stand-in on PATH.
"""

import os
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from solidlsp.language_servers.ocaml_lsp_server import OcamlLanguageServer
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """The OCaml project opam runs in."""
    root = tmp_path / "project"
    root.mkdir()
    return root


def test_the_ocaml_version_is_the_one_ocaml_version_prints(stand_ins: StandIns, project: Path) -> None:
    opam = stand_ins.answering("opam", stdout="The OCaml toplevel, version 5.2.1\n")

    assert OcamlLanguageServer._detect_ocaml_version(str(project)) == (5, 2, 1)
    assert_started_through_brain_supervise(opam)


def test_the_lsp_version_is_the_one_opam_list_prints(stand_ins: StandIns, project: Path) -> None:
    opam = stand_ins.answering("opam", stdout="1.23.0\n")

    assert OcamlLanguageServer._detect_lsp_version(str(project)) == (1, 23, 0)
    assert_started_through_brain_supervise(opam)


def test_the_ocaml_lsp_executable_is_the_one_opam_finds(stand_ins: StandIns, project: Path, tmp_path: Path) -> None:
    # an opam that lists ocaml-lsp-server as installed and finds its executable
    ocamllsp = tmp_path / "switch" / "bin" / "ocamllsp"
    ocamllsp.parent.mkdir(parents=True)
    ocamllsp.write_text("#!/bin/sh\n")
    opam = stand_ins.add(
        "opam",
        f"""
        case "$1" in
            list) echo 'ocaml-lsp-server  1.23.0  LSP Server for OCaml' ;;
            exec) echo {shlex.quote(str(ocamllsp))} ;;
        esac
        """,
    )

    assert OcamlLanguageServer._ensure_ocaml_lsp_installed(str(project)) == str(ocamllsp)
    assert_started_through_brain_supervise(opam)


def test_on_windows_the_ocaml_lsp_executable_is_the_one_opam_finds_with_where(
    stand_ins: StandIns, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the branch of a Windows host, the only one that asks opam through where
    monkeypatch.setattr("solidlsp.language_servers.ocaml_lsp_server.platform", SimpleNamespace(system=lambda: "Windows"))
    ocamllsp = tmp_path / "switch" / "bin" / "ocamllsp"
    ocamllsp.parent.mkdir(parents=True)
    ocamllsp.write_text("#!/bin/sh\n")
    opam = stand_ins.add(
        "opam",
        f"""
        case "$1 $3 $4" in
            list*) echo 'ocaml-lsp-server  1.23.0  LSP Server for OCaml' ;;
            "exec where ocamllsp") echo {shlex.quote(str(ocamllsp))} ;;
        esac
        """,
    )

    assert OcamlLanguageServer._ensure_ocaml_lsp_installed(str(project)) == str(ocamllsp)
    assert_started_through_brain_supervise(opam)


@pytest.mark.parametrize(("exit_code", "built"), [(0, True), (1, False)])
def test_the_index_is_built_where_dune_build_succeeds(stand_ins: StandIns, project: Path, exit_code: int, built: bool) -> None:
    opam = stand_ins.answering("opam", stderr="Error: Unbound module Foo\n" if exit_code else "", exit_code=exit_code)

    assert OcamlLanguageServer._build_ocaml_index_static(str(project)) is built
    assert_started_through_brain_supervise(opam)
