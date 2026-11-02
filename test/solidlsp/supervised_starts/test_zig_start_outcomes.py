"""
The Zig family's runs of ``zig`` and ``zls`` through brain-supervise, where the program fails or is not installed: the
version probes of :class:`ZigLanguageServer`, each run against a stand-in on PATH.
"""

import os

import pytest

from solidlsp.language_servers.zls import ZigLanguageServer
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


def test_there_is_no_zig_version_where_zig_fails(stand_ins: StandIns) -> None:
    zig = stand_ins.answering("zig", stderr="zig: unable to find the standard library\n", exit_code=1)

    assert ZigLanguageServer._get_zig_version() is None
    assert_started_through_brain_supervise(zig)


def test_there_is_no_zig_version_where_zig_is_not_installed(stand_ins: StandIns) -> None:
    assert ZigLanguageServer._get_zig_version() is None


def test_there_is_no_zls_version_where_zls_fails(stand_ins: StandIns) -> None:
    zls = stand_ins.answering("zls", stderr="zls: unable to find zig\n", exit_code=1)

    assert ZigLanguageServer._get_zls_version() is None
    assert_started_through_brain_supervise(zls)


def test_there_is_no_zls_version_where_zls_is_not_installed(stand_ins: StandIns) -> None:
    assert ZigLanguageServer._get_zls_version() is None
