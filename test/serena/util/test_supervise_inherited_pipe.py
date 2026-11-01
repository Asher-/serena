"""
A start that names no stream, from a process whose own stream is a pipe, as serena's are when it serves stdio: brain-supervise
refuses a pipe as a started process's stream, so the output goes to the record's file and the start succeeds.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from solidlsp.util import supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")

_STARTER = textwrap.dedent(
    """
    import sys
    from solidlsp.util import supervise
    completed = supervise.run(["/bin/sh", "-c", "echo from-the-child"], call_id="")
    print("returned", completed.returncode, flush=True)
    """
)


def test_a_start_with_this_process_stdout_a_pipe_succeeds_and_writes_nothing_to_the_pipe(tmp_path: Path) -> None:
    done = subprocess.run(
        [sys.executable, "-c", _STARTER],
        env={**os.environ, supervise.ROOT_ENV: str(tmp_path / "restart")},
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout == "returned 0\n"
