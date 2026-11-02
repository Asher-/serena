"""
The Perl family's runs of ``perl`` through brain-supervise, where ``perl`` fails or is not installed: the version probes
of :class:`PerlLanguageServer`, each run against a stand-in on PATH.
"""

import os

import pytest

from solidlsp.language_servers.perl_language_server import PerlLanguageServer
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


def test_there_is_no_perl_version_where_perl_fails(stand_ins: StandIns) -> None:
    perl = stand_ins.answering("perl", stderr="perl: warning: Setting locale failed.\n", exit_code=1)

    assert PerlLanguageServer._get_perl_version() is None
    assert_started_through_brain_supervise(perl)


def test_there_is_no_perl_version_where_perl_is_not_installed(stand_ins: StandIns) -> None:
    assert PerlLanguageServer._get_perl_version() is None


def test_there_is_no_language_server_version_where_perl_cannot_load_the_module(stand_ins: StandIns) -> None:
    perl = stand_ins.answering("perl", stderr="Can't locate Perl/LanguageServer.pm in @INC\n", exit_code=2)

    assert PerlLanguageServer._get_perl_language_server_version() is None
    assert_started_through_brain_supervise(perl)


def test_there_is_no_language_server_version_where_perl_is_not_installed(stand_ins: StandIns) -> None:
    assert PerlLanguageServer._get_perl_language_server_version() is None
