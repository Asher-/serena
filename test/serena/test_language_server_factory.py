"""tests for the language server factory's hand-over of a project's per-path ignore spec, with no language server process started."""

from pathlib import Path
from unittest.mock import Mock, patch, sentinel

from pathspec import PathSpec

from serena.config.serena_config import ProjectConfig
from serena.ls_manager import LanguageServerFactory
from serena.util.file_system import IgnoreSpecs
from solidlsp import SolidLanguageServer
from solidlsp.language_servers.pyright_server import PyrightServer
from solidlsp.ls_config import LanguageServerId


def _factory(tmp_path: Path, ignore_spec_of, **kwargs) -> LanguageServerFactory:
    return LanguageServerFactory(
        project_root=str(tmp_path),
        project_config=ProjectConfig(project_name="p", language_servers=[LanguageServerId.PYTHON]),
        project_data_path=str(tmp_path / "data"),
        encoding="utf-8",
        ignore_spec_of=ignore_spec_of,
        **kwargs,
    )


def test_the_factory_hands_its_spec_function_to_each_server_configuration(tmp_path: Path) -> None:
    def spec_of(relative_path: str) -> PathSpec:
        return PathSpec([])

    factory = _factory(tmp_path, spec_of, trace_lsp_communication=True, is_project_trusted=False)
    with patch.object(SolidLanguageServer, "create", return_value=sentinel.server) as create:
        assert factory.create_language_server(LanguageServerId.PYTHON) is sentinel.server

    config = create.call_args.args[0]
    assert create.call_args.args[1] == str(tmp_path)
    assert config.ignore_spec_of is spec_of
    assert config.ls_id == LanguageServerId.PYTHON
    assert config.trace_lsp_communication
    assert not config.is_project_trusted
    assert config.encoding == "utf-8"


def test_a_created_server_decides_ignores_by_the_projects_gitignore_chain(tmp_path: Path) -> None:
    """The factory's server, built by the real constructor, ignores what the project's .gitignore chain ignores."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / ".gitignore").write_text("generated.py\n")
    (tmp_path / "pkg" / "generated.py").write_text("x = 1\n")
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.py").write_text("x = 1\n")
    ignore_specs = IgnoreSpecs(str(tmp_path), [".git", "build/"], read_gitignore_files=True)
    factory = _factory(tmp_path, ignore_specs.spec_of)

    with patch.object(PyrightServer, "_create_language_server_interface", Mock(return_value=Mock())):
        server = factory.create_language_server(LanguageServerId.PYTHON)

    assert isinstance(server, PyrightServer)
    assert server.is_ignored_path("pkg/generated.py")
    assert server.is_ignored_path("build/out.py")
    assert not server.is_ignored_path("pkg/mod.py")
