"""
The serena home of the test processes: no test reads or writes the developer's configuration
(bug://serena/config/file/write/atomicity).
"""

import os
from pathlib import Path

from serena.config.serena_config import SerenaPaths
from test.conftest import SCRATCH_ROOT


def test_the_tests_run_under_a_serena_home_of_their_own() -> None:
    assert Path(os.environ["SERENA_HOME"]).is_relative_to(SCRATCH_ROOT)
    assert Path(SerenaPaths().serena_user_home_dir).is_relative_to(SCRATCH_ROOT)
