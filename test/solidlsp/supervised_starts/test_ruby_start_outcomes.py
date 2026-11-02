"""
The Ruby family's runs of ``ruby`` and ``gem`` through brain-supervise, where a program fails or is not installed: the
runtime setup of :class:`RubyLsp` and of :class:`Solargraph`, each run against stand-ins on PATH over a project that
names no Ruby version manager and no Bundler.
"""

import os
from pathlib import Path

import pytest

from solidlsp.language_servers.ruby_lsp import RubyLsp
from solidlsp.language_servers.solargraph import Solargraph
from solidlsp.ls_config import Language, LanguageServerConfig
from solidlsp.settings import SolidLSPSettings
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")

_RUBY_VERSION = "ruby 3.3.0 (2023-12-25 revision 5124f9ac75) [arm64-darwin23]\n"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A Ruby project with no version file and no Gemfile."""
    root = tmp_path / "project"
    root.mkdir()
    return root


def test_ruby_lsp_setup_fails_where_ruby_is_not_installed(stand_ins: StandIns, project: Path, tmp_path: Path) -> None:
    settings = SolidLSPSettings(solidlsp_dir=str(tmp_path / "solidlsp"))

    with pytest.raises(RuntimeError) as caught:
        RubyLsp._setup_runtime_dependencies(LanguageServerConfig(code_language=Language.RUBY), str(project), settings)
    assert "Ruby is not installed or not found in PATH" in str(caught.value)


def test_ruby_lsp_setup_fails_with_what_a_failing_gem_install_wrote(stand_ins: StandIns, project: Path, tmp_path: Path) -> None:
    ruby = stand_ins.answering("ruby", stdout=_RUBY_VERSION)
    gem = stand_ins.answering("gem", stderr="ERROR:  Could not find a valid gem 'ruby-lsp' (= 0.17.0)\n", exit_code=2)
    settings = SolidLSPSettings(solidlsp_dir=str(tmp_path / "solidlsp"))

    with pytest.raises(RuntimeError) as caught:
        RubyLsp._setup_runtime_dependencies(LanguageServerConfig(code_language=Language.RUBY), str(project), settings)
    assert "Failed to install ruby-lsp: ERROR:  Could not find a valid gem 'ruby-lsp' (= 0.17.0)" in str(caught.value)
    assert_started_through_brain_supervise(ruby, gem)


def test_solargraph_setup_fails_where_ruby_fails(stand_ins: StandIns, project: Path) -> None:
    # a ruby that writes no stderr, which Solargraph would decode as bytes though its run reads text
    ruby = stand_ins.answering("ruby", exit_code=1)

    with pytest.raises(RuntimeError) as caught:
        Solargraph._setup_runtime_dependencies(LanguageServerConfig(code_language=Language.RUBY), str(project))
    assert "Error checking Ruby installation: Unknown error." in str(caught.value)
    assert_started_through_brain_supervise(ruby)


def test_solargraph_setup_fails_where_ruby_is_not_installed(stand_ins: StandIns, project: Path) -> None:
    with pytest.raises(RuntimeError) as caught:
        Solargraph._setup_runtime_dependencies(LanguageServerConfig(code_language=Language.RUBY), str(project))
    assert "Ruby is not installed or not found in PATH" in str(caught.value)


def test_solargraph_setup_fails_with_what_a_failing_gem_install_wrote(stand_ins: StandIns, project: Path) -> None:
    # a gem that lists solargraph as not installed and fails to install it
    ruby = stand_ins.answering("ruby", stdout=_RUBY_VERSION)
    gem = stand_ins.add(
        "gem",
        """
        case "$1" in
            list) echo false ;;
            install) echo 'ERROR:  Could not find a valid gem solargraph' >&2; exit 2 ;;
        esac
        """,
    )

    with pytest.raises(RuntimeError) as caught:
        Solargraph._setup_runtime_dependencies(LanguageServerConfig(code_language=Language.RUBY), str(project))
    assert "Failed to check or install Solargraph: ERROR:  Could not find a valid gem solargraph" in str(caught.value)
    assert_started_through_brain_supervise(ruby, gem)
