"""
:func:`subprocess_util.subprocess_run` runs a program through brain-supervise, with a stand-in on PATH for the program.
"""

import os

import pytest

from solidlsp.util import subprocess_util, supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


def test_a_run_answers_with_the_exit_code_and_the_text_the_program_wrote(stand_ins: StandIns) -> None:
    uv = stand_ins.answering("uv", stdout="Installed 1 executable: tool\n", stderr="warning: no cache\n", exit_code=3)

    result = subprocess_util.subprocess_run(["uv", "tool", "install", "tool==1.0"])

    assert (result.returncode, result.stdout, result.stderr) == (3, "Installed 1 executable: tool\n", "warning: no cache\n")
    assert_started_through_brain_supervise(uv)
