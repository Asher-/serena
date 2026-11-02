"""
The R family's runs of ``R`` through brain-supervise, where ``R`` fails or is not installed: the installation check of
:class:`RLanguageServer`, run against a stand-in on PATH.
"""

import os

import pytest

from solidlsp.language_servers.r_language_server import RLanguageServer
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


def test_the_check_fails_where_r_version_fails(stand_ins: StandIns) -> None:
    r = stand_ins.answering("R", stderr="R: cannot find the R home directory\n", exit_code=1)

    with pytest.raises(RuntimeError) as caught:
        RLanguageServer._check_r_installation()
    assert "R is not installed or not in PATH" in str(caught.value)
    assert_started_through_brain_supervise(r)


def test_the_check_fails_where_r_is_not_installed(stand_ins: StandIns) -> None:
    with pytest.raises(RuntimeError) as caught:
        RLanguageServer._check_r_installation()
    assert "R is not installed. Please install R from https://www.r-project.org/" in str(caught.value)
