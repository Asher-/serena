"""
The Nix family starts ``nixd``, ``nix`` and ``nix-env`` through brain-supervise: the version probe, the install and the
verification of :class:`NixLanguageServer`, each run against stand-ins on PATH.
"""

import os
import shlex

import pytest

from solidlsp.language_servers.nixd_ls import NixLanguageServer
from solidlsp.settings import SolidLSPSettings
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


def _provider() -> NixLanguageServer.DependencyProvider:
    """
    :return: the provider of the nixd launch command, which reads neither its settings nor its resources directory to
        find, install or verify nixd.
    """
    return NixLanguageServer.DependencyProvider(SolidLSPSettings.CustomLSSettings({}), "")


def _installing(stand_ins: StandIns) -> str:
    """
    :param stand_ins: the test's stand-ins.
    :return: shell that puts an executable nixd on PATH, as an install of nixd does.
    """
    nixd = shlex.quote(str(stand_ins.bin / "nixd"))
    return f"/usr/bin/touch {nixd}; /bin/chmod +x {nixd}"


def test_the_nixd_version_is_what_nixd_version_prints(stand_ins: StandIns) -> None:
    nixd = stand_ins.answering("nixd", stdout="nixd 2.3.1\n")

    assert NixLanguageServer.DependencyProvider._get_nixd_version() == "nixd 2.3.1"
    assert_started_through_brain_supervise(nixd)


def test_nixd_is_the_one_nix_profile_install_put_on_path(stand_ins: StandIns) -> None:
    nix = stand_ins.add("nix", f'if [ "$1" = profile ]; then {_installing(stand_ins)}; fi')

    assert NixLanguageServer.DependencyProvider._install_nixd_with_nix() == str(stand_ins.bin / "nixd")
    assert_started_through_brain_supervise(nix)


def test_nixd_is_the_one_nix_env_put_on_path_where_nix_profile_install_fails(stand_ins: StandIns) -> None:
    nix = stand_ins.answering("nix", stderr="error: profile is not writable\n", exit_code=1)
    nix_env = stand_ins.add("nix-env", _installing(stand_ins))

    assert NixLanguageServer.DependencyProvider._install_nixd_with_nix() == str(stand_ins.bin / "nixd")
    assert_started_through_brain_supervise(nix, nix_env)


def test_nixd_setup_fails_with_what_a_nixd_that_does_not_run_wrote(stand_ins: StandIns) -> None:
    stand_ins.answering("nix")
    nixd = stand_ins.answering("nixd", stderr="nixd: unknown option '--version'\n", exit_code=1)

    with pytest.raises(RuntimeError) as caught:
        _provider()._get_or_install_core_dependency()
    assert "nixd: unknown option '--version'" in str(caught.value)
    assert_started_through_brain_supervise(nixd)
