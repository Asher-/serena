"""
The TypeScript family starts ``npm`` through brain-supervise: the dependency install of the TypeScript structural bridge,
run against a stand-in on PATH.
"""

import os
from pathlib import Path

import pytest

from solidlsp.structural.backends import typescript as typescript_backend
from solidlsp.structural.errors import ParseError
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


def test_a_failed_dependency_install_raises_with_what_npm_install_wrote(
    stand_ins: StandIns, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "typescript_bridge"
    monkeypatch.setattr(typescript_backend, "_BRIDGE_PACKAGE_DIR", package)
    monkeypatch.setattr(typescript_backend, "_BRIDGE_NODE_MODULES", package / "node_modules")
    npm = stand_ins.answering(
        "npm", stderr="npm ERR! code E404\nnpm ERR! 404 Not Found - GET https://registry.npmjs.org/typescript\n", exit_code=1
    )

    with pytest.raises(ParseError) as caught:
        typescript_backend._install_dependencies_if_needed()
    assert "npm ERR! 404 Not Found - GET https://registry.npmjs.org/typescript" in caught.value.detail
    assert_started_through_brain_supervise(npm)
