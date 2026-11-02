"""
The TypeScript family's run of ``npm`` through brain-supervise, where the dependency install of the TypeScript
structural bridge succeeds, run against a stand-in on PATH.
"""

import os
from pathlib import Path

import pytest

from solidlsp.structural.backends import typescript as typescript_backend
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


def test_the_dependencies_are_the_node_modules_npm_install_wrote_in_the_package_directory(
    stand_ins: StandIns, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "typescript_bridge"
    monkeypatch.setattr(typescript_backend, "_BRIDGE_PACKAGE_DIR", package)
    monkeypatch.setattr(typescript_backend, "_BRIDGE_NODE_MODULES", package / "node_modules")
    # an npm that writes node_modules under the directory it runs in
    npm = stand_ins.add("npm", "/bin/mkdir node_modules")

    typescript_backend._install_dependencies_if_needed()

    assert (package / "node_modules").is_dir()
    assert_started_through_brain_supervise(npm)
