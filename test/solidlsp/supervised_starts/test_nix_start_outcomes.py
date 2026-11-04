"""
The Nix family's runs of ``nixd``, ``nix`` and ``nix-env`` through brain-supervise, where a program fails, cannot be
started or outlasts its timeout, and where ``nixd`` runs: the version probe, the install and the verification of
:class:`NixLanguageServer`, each run against stand-ins on PATH. The 10 minutes ``nix`` is given to install are not
waited out: the run that would time out is replaced by one that does.
"""

import os

import pytest

from solidlsp.language_servers.nixd_ls import NixLanguageServer
from solidlsp.settings import SolidLSPSettings
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise, time_out_runs

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")


def _provider() -> NixLanguageServer.DependencyProvider:
    """
    :return: the provider of the nixd launch command, which reads neither its settings nor its resources directory to
        find, install or verify nixd.
    """
    return NixLanguageServer.DependencyProvider(SolidLSPSettings.CustomLSSettings({}), "")


def test_there_is_no_nixd_version_where_nixd_fails(stand_ins: StandIns) -> None:
    nixd = stand_ins.answering("nixd", stderr="nixd: unknown option '--version'\n", exit_code=1)

    assert NixLanguageServer.DependencyProvider._get_nixd_version() is None
    assert_started_through_brain_supervise(nixd)


def test_there_is_no_nixd_version_where_nixd_is_not_installed(stand_ins: StandIns) -> None:
    assert NixLanguageServer.DependencyProvider._get_nixd_version() is None


def test_nixd_is_not_installed_where_both_nix_profile_install_and_nix_env_fail(
    stand_ins: StandIns, caplog: pytest.LogCaptureFixture
) -> None:
    nix = stand_ins.answering("nix", stderr="error: profile is not writable\n", exit_code=1)
    nix_env = stand_ins.answering("nix-env", stderr="error: attribute 'nixd' in selection path 'nixpkgs.nixd' not found\n", exit_code=1)

    assert NixLanguageServer.DependencyProvider._install_nixd_with_nix() is None
    assert "Failed to install nixd: error: attribute 'nixd' in selection path 'nixpkgs.nixd' not found" in caplog.text
    assert_started_through_brain_supervise(nix, nix_env)


def test_nixd_is_not_installed_where_nix_profile_install_times_out(
    stand_ins: StandIns, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    stand_ins.answering("nix")
    timed_out = time_out_runs(monkeypatch)

    assert NixLanguageServer.DependencyProvider._install_nixd_with_nix() is None
    assert [(run.args, run.timeout) for run in timed_out] == [(["nix", "profile", "install", "github:nix-community/nixd"], 600)]
    assert "Nix install timed out after 10 minutes" in caplog.text


def test_nixd_is_not_installed_where_nix_cannot_be_started(stand_ins: StandIns, caplog: pytest.LogCaptureFixture) -> None:
    stand_ins.unrunnable("nix")

    assert NixLanguageServer.DependencyProvider._install_nixd_with_nix() is None
    assert "Error installing nixd with nix" in caplog.text
    assert "supervise: start" in caplog.text


def test_nixd_is_the_one_on_path_where_it_runs(stand_ins: StandIns) -> None:
    stand_ins.answering("nix")
    nixd = stand_ins.answering("nixd", stdout="nixd 2.3.1\n")

    assert _provider()._get_or_install_core_dependency() == str(nixd.path)
    assert_started_through_brain_supervise(nixd)


def test_nixd_setup_fails_where_the_nixd_on_path_cannot_be_started(stand_ins: StandIns) -> None:
    stand_ins.answering("nix")
    stand_ins.unrunnable("nixd")

    with pytest.raises(RuntimeError, match=r"^Failed to verify nixd installation: ") as caught:
        _provider()._get_or_install_core_dependency()
    assert "nixd failed to run" not in str(caught.value)
    assert caught.value.__cause__ is not None
