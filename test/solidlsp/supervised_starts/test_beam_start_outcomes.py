"""
The BEAM family's probes of ``elixir``, ``erl`` and ``rebar3`` through brain-supervise, where the program fails or is not
installed: the toolchain probes of :class:`ElixirTools` and :class:`ErlangLanguageServer`, each run against stand-ins on
PATH.
"""

import os

import pytest

from solidlsp.language_servers.elixir_tools.elixir_tools import ElixirTools
from solidlsp.language_servers.erlang_language_server import ErlangLanguageServer
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


def test_there_is_no_elixir_version_where_elixir_fails(stand_ins: StandIns) -> None:
    elixir = stand_ins.answering("elixir", stderr="elixir: erlang runtime not found\n", exit_code=1)

    assert ElixirTools._get_elixir_version() is None
    assert_started_through_brain_supervise(elixir)


def test_there_is_no_elixir_version_where_elixir_is_not_installed(stand_ins: StandIns) -> None:
    assert ElixirTools._get_elixir_version() is None


def test_erlang_is_not_installed_where_erl_is_not_on_path(stand_ins: StandIns) -> None:
    server = ErlangLanguageServer.__new__(ErlangLanguageServer)

    assert server._check_erlang_installation() is False


def test_there_is_no_erlang_version_where_erl_fails(stand_ins: StandIns) -> None:
    erl = stand_ins.answering("erl", stderr="erl: cannot start the emulator\n", exit_code=1)

    assert ErlangLanguageServer._get_erlang_version() is None
    assert_started_through_brain_supervise(erl)


def test_there_is_no_erlang_version_where_erl_is_not_installed(stand_ins: StandIns) -> None:
    assert ErlangLanguageServer._get_erlang_version() is None


def test_rebar3_is_not_available_where_it_is_not_on_path(stand_ins: StandIns) -> None:
    assert ErlangLanguageServer._check_rebar3_available() is False
