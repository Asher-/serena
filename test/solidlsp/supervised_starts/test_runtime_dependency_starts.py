"""
:class:`RuntimeDependencyCollection` runs a dependency's install command under the shell through brain-supervise, with a
stand-in on PATH for the program the command names.
"""

import os
import shlex
import subprocess
from pathlib import Path

import pytest

from solidlsp.language_servers.common import RuntimeDependencyCollection
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


@pytest.fixture
def directory(tmp_path: Path) -> Path:
    """The directory the install command runs in."""
    path = tmp_path / "dependency"
    path.mkdir()
    return path


def test_an_install_command_runs_in_the_directory_given(stand_ins: StandIns, directory: Path, tmp_path: Path) -> None:
    witness = tmp_path / "ran-in"
    make = stand_ins.add("make", f"/bin/pwd -P > {shlex.quote(str(witness))}")

    RuntimeDependencyCollection._run_command(["make", "install"], str(directory))

    assert witness.read_text() == f"{directory.resolve()}\n"
    assert_started_through_brain_supervise(make)


def test_a_failed_install_command_raises_with_its_exit_code_and_what_it_wrote(stand_ins: StandIns, directory: Path) -> None:
    make = stand_ins.answering("make", stdout="cc -o tool tool.c\n", stderr="tool.c:1: error: expected ';'\n", exit_code=2)

    with pytest.raises(subprocess.CalledProcessError) as caught:
        RuntimeDependencyCollection._run_command("make install", str(directory))
    assert (caught.value.returncode, caught.value.output) == (2, b"cc -o tool tool.c\ntool.c:1: error: expected ';'\n")
    assert_started_through_brain_supervise(make)
