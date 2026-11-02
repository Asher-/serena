"""
:func:`subprocess_check_output` starts its program through brain-supervise, run against stand-ins on PATH.
"""

import os
from pathlib import Path

import pytest

from serena.util.shell import subprocess_check_output
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


def test_the_output_is_what_the_program_printed_without_the_whitespace_around_it(stand_ins: StandIns) -> None:
    tool = stand_ins.answering("tool", stdout="  3.1.4  \n")

    assert subprocess_check_output(["tool", "--version"]) == "3.1.4"
    assert_started_through_brain_supervise(tool)


def test_the_output_is_what_the_program_printed_as_it_printed_it_where_stripping_is_off(stand_ins: StandIns) -> None:
    tool = stand_ins.answering("tool", stdout="  3.1.4  \n")

    assert subprocess_check_output(["tool", "--version"], strip=False) == "  3.1.4  \n"
    assert_started_through_brain_supervise(tool)


def test_the_output_is_decoded_with_the_encoding_given(stand_ins: StandIns) -> None:
    # a tool that prints the byte 0xe9, which is "é" in latin-1 and no valid UTF-8
    tool = stand_ins.add("tool", "/usr/bin/printf '\\351'")

    assert subprocess_check_output(["tool"], encoding="latin-1") == "é"
    assert_started_through_brain_supervise(tool)


def test_the_program_runs_in_the_working_directory_given(stand_ins: StandIns, tmp_path: Path) -> None:
    tool = stand_ins.add("tool", "/bin/pwd -P")
    directory = tmp_path / "work"
    directory.mkdir()

    assert subprocess_check_output(["tool"], cwd=str(directory)) == str(directory.resolve())
    assert_started_through_brain_supervise(tool)
