"""
The PHP family starts ``php`` through brain-supervise: the PHP version check of :class:`PhpactorServer.DependencyProvider`,
run against a stand-in on PATH.
"""

import os
from pathlib import Path

import pytest

from solidlsp.language_servers.phpactor import PhpactorServer
from solidlsp.settings import SolidLSPSettings
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


@pytest.fixture
def resources(tmp_path: Path) -> Path:
    """The provider's resources directory, which already holds the Phpactor PHAR."""
    path = tmp_path / "resources"
    path.mkdir()
    (path / "phpactor.phar").write_text("<?php\n")
    return path


def test_phpactor_refuses_a_php_older_than_8_1(stand_ins: StandIns, resources: Path) -> None:
    php = stand_ins.answering("php", stdout="PHP 7.4.33 (cli) (built: Nov  2 2023 16:00:00) ( NTS )\n")
    provider = PhpactorServer.DependencyProvider(SolidLSPSettings.CustomLSSettings({}), str(resources))

    with pytest.raises(RuntimeError) as caught:
        provider._get_or_install_core_dependency()
    assert "PHP 7.4 detected" in str(caught.value)
    assert_started_through_brain_supervise(php)


def test_phpactor_runs_from_its_phar_on_php_8_1_or_later(stand_ins: StandIns, resources: Path) -> None:
    php = stand_ins.answering("php", stdout="PHP 8.3.4 (cli) (built: Mar 12 2024 23:42:53) ( NTS )\n")
    provider = PhpactorServer.DependencyProvider(SolidLSPSettings.CustomLSSettings({}), str(resources))

    assert provider._get_or_install_core_dependency() == str(resources / "phpactor.phar")
    assert_started_through_brain_supervise(php)
