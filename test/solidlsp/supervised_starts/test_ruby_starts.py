"""
The Ruby family starts ``ruby`` and ``gem`` through brain-supervise: the runtime setup of :class:`RubyLsp` and of
:class:`Solargraph`, each run against stand-ins on PATH over a project that names no Ruby version manager and no Bundler.
"""

import os
import shlex
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


def test_ruby_lsp_setup_fails_with_what_a_failing_ruby_wrote(stand_ins: StandIns, project: Path, tmp_path: Path) -> None:
    ruby = stand_ins.answering("ruby", stderr="ruby: cannot load the standard library\n", exit_code=1)
    settings = SolidLSPSettings(solidlsp_dir=str(tmp_path / "solidlsp"))

    with pytest.raises(RuntimeError) as caught:
        RubyLsp._setup_runtime_dependencies(LanguageServerConfig(code_language=Language.RUBY), str(project), settings)
    assert "ruby: cannot load the standard library" in str(caught.value)
    assert_started_through_brain_supervise(ruby)


def test_ruby_lsp_is_launched_from_where_gem_install_put_it(stand_ins: StandIns, project: Path, tmp_path: Path) -> None:
    # a gem that installs ruby-lsp onto PATH
    ruby_lsp = shlex.quote(str(stand_ins.bin / "ruby-lsp"))
    ruby = stand_ins.answering("ruby", stdout=_RUBY_VERSION)
    gem = stand_ins.add("gem", f'if [ "$1" = install ]; then /usr/bin/touch {ruby_lsp}; /bin/chmod +x {ruby_lsp}; fi')
    settings = SolidLSPSettings(solidlsp_dir=str(tmp_path / "solidlsp"))

    command = RubyLsp._setup_runtime_dependencies(LanguageServerConfig(code_language=Language.RUBY), str(project), settings)

    assert command == [str(stand_ins.bin / "ruby-lsp")]
    assert_started_through_brain_supervise(ruby, gem)


@pytest.mark.parametrize(("listed", "installed"), [("false", True), ("true", False)])
def test_solargraph_is_installed_exactly_where_gem_list_says_it_is_not(
    stand_ins: StandIns, project: Path, tmp_path: Path, listed: str, installed: bool
) -> None:
    # a gem that answers whether solargraph is installed, and marks an install
    marker = tmp_path / "gem-installed"
    ruby = stand_ins.answering("ruby", stdout=_RUBY_VERSION)
    gem = stand_ins.add(
        "gem",
        f"""
        case "$1" in
            list) echo {listed} ;;
            install) /usr/bin/touch {shlex.quote(str(marker))} ;;
        esac
        """,
    )

    command = Solargraph._setup_runtime_dependencies(LanguageServerConfig(code_language=Language.RUBY), str(project))

    assert command == "gem exec solargraph"
    assert marker.exists() is installed
    assert_started_through_brain_supervise(ruby, gem)
