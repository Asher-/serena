"""
The Zig family starts ``zig`` and ``zls`` through brain-supervise: the version probes of :class:`ZigLanguageServer`, each
run against a stand-in on PATH.
"""

import os

import pytest

from solidlsp.language_servers.zls import ZigLanguageServer
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


def test_the_zig_version_is_what_zig_version_prints(stand_ins: StandIns) -> None:
    zig = stand_ins.answering("zig", stdout="0.13.0\n")

    assert ZigLanguageServer._get_zig_version() == "0.13.0"
    assert_started_through_brain_supervise(zig)


def test_the_zls_version_is_what_zls_version_prints(stand_ins: StandIns) -> None:
    zls = stand_ins.answering("zls", stdout="0.13.0\n")

    assert ZigLanguageServer._get_zls_version() == "0.13.0"
    assert_started_through_brain_supervise(zls)
