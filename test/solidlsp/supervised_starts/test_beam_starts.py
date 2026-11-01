"""
The BEAM family starts ``elixir``, ``erl`` and ``rebar3`` through brain-supervise: the toolchain probes of
:class:`ElixirTools` and :class:`ErlangLanguageServer`, each run against stand-ins on PATH.
"""

import os

import pytest

from solidlsp.language_servers.elixir_tools.elixir_tools import ElixirTools
from solidlsp.language_servers.erlang_language_server import ErlangLanguageServer
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


def test_the_elixir_version_is_what_elixir_version_prints(stand_ins: StandIns) -> None:
    elixir = stand_ins.answering("elixir", stdout="Erlang/OTP 26 [erts-14.2.1]\n\nElixir 1.16.0 (compiled with Erlang/OTP 26)\n")

    assert ElixirTools._get_elixir_version() == "Erlang/OTP 26 [erts-14.2.1]\n\nElixir 1.16.0 (compiled with Erlang/OTP 26)"
    assert_started_through_brain_supervise(elixir)


@pytest.mark.parametrize(("exit_code", "installed"), [(0, True), (1, False)])
def test_erlang_is_installed_where_erl_runs(stand_ins: StandIns, exit_code: int, installed: bool) -> None:
    erl = stand_ins.answering("erl", stderr="Erlang (SMP,ASYNC_THREADS) (BEAM) emulator version 14.2.1\n", exit_code=exit_code)
    server = ErlangLanguageServer.__new__(ErlangLanguageServer)

    assert server._check_erlang_installation() is installed
    assert_started_through_brain_supervise(erl)


def test_the_erlang_version_is_what_erl_version_writes_to_stderr(stand_ins: StandIns) -> None:
    erl = stand_ins.answering("erl", stderr="Erlang (SMP,ASYNC_THREADS) (BEAM) emulator version 14.2.1\n")

    assert ErlangLanguageServer._get_erlang_version() == "Erlang (SMP,ASYNC_THREADS) (BEAM) emulator version 14.2.1"
    assert_started_through_brain_supervise(erl)


@pytest.mark.parametrize(("exit_code", "available"), [(0, True), (1, False)])
def test_rebar3_is_available_where_rebar3_version_runs(stand_ins: StandIns, exit_code: int, available: bool) -> None:
    rebar3 = stand_ins.answering("rebar3", stdout="rebar 3.22.1 on Erlang/OTP 26 Erts 14.2.1\n", exit_code=exit_code)

    assert ErlangLanguageServer._check_rebar3_available() is available
    assert_started_through_brain_supervise(rebar3)
