"""
The R family starts ``R`` through brain-supervise: the installation check of :class:`RLanguageServer`, run against a
stand-in on PATH.
"""

import os

import pytest

from solidlsp.language_servers.r_language_server import RLanguageServer
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


def test_the_check_passes_where_r_runs_and_loads_the_languageserver_package(stand_ins: StandIns) -> None:
    r = stand_ins.answering("R", stdout='R version 4.4.1 (2024-06-14) -- "Race for Your Life"\n')

    RLanguageServer._check_r_installation()

    assert_started_through_brain_supervise(r)


def test_the_check_fails_where_r_runs_but_cannot_load_the_languageserver_package(stand_ins: StandIns) -> None:
    r = stand_ins.add(
        "R",
        """
        case "$1" in
            --version) echo 'R version 4.4.1 (2024-06-14)' ;;
            *) echo "Error in library(languageserver) : there is no package called 'languageserver'" >&2; exit 1 ;;
        esac
        """,
    )

    with pytest.raises(RuntimeError) as caught:
        RLanguageServer._check_r_installation()
    assert "languageserver package is not installed" in str(caught.value)
    assert_started_through_brain_supervise(r)
