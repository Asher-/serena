"""
The Perl family starts ``perl`` through brain-supervise: the version probes of :class:`PerlLanguageServer`, each run
against a stand-in on PATH.
"""

import os

import pytest

from solidlsp.language_servers.perl_language_server import PerlLanguageServer
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


def test_the_perl_version_is_what_perl_v_prints(stand_ins: StandIns) -> None:
    perl = stand_ins.answering("perl", stdout="\nThis is perl 5, version 38, subversion 2 (v5.38.2)\n")

    assert PerlLanguageServer._get_perl_version() == "This is perl 5, version 38, subversion 2 (v5.38.2)"
    assert_started_through_brain_supervise(perl)


def test_the_language_server_version_is_what_perl_prints_of_it(stand_ins: StandIns) -> None:
    perl = stand_ins.answering("perl", stdout="2.6.2")

    assert PerlLanguageServer._get_perl_language_server_version() == "2.6.2"
    assert_started_through_brain_supervise(perl)
