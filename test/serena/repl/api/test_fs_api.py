"""
Tests for the file system facade API.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from serena.agent import SerenaAgent
from serena.config.serena_config import SerenaConfig
from serena.project import Project
from serena.repl.api.fs_api import FsApi
from serena.repl.facade import ApiScope, Facade


@pytest.fixture
def project(tmp_path: Path) -> Project:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = foo(1)\ny = foo(2)\nz = 3\n", encoding="utf-8")
    (tmp_path / "src" / "b.txt").write_text("foo in text\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# readme\n", encoding="utf-8")
    return Project.load(str(tmp_path), serena_config=SerenaConfig(gui_log_window=False, web_dashboard=False))


@pytest.fixture
def api(project: Project) -> FsApi:
    agent = MagicMock()
    agent.get_active_project_or_raise.return_value = project
    agent.serena_config.default_max_tool_answer_chars = 10000
    return FsApi(agent)


def test_facade_exposes_file_operations(api: FsApi) -> None:
    facade = Facade.from_api(api, ApiScope())
    assert facade.name == "fs"
    assert set(facade.enabled_method_names) == {"read_file", "create_text_file", "list_dir", "find_file", "search_for_pattern"}
    assert {name for name in facade.enabled_method_names if facade.get_method(name).info.can_edit} == {"create_text_file"}


def test_read_file(api: FsApi) -> None:
    content = api.read_file("src/a.py")
    assert content.lines == ["x = foo(1)", "y = foo(2)", "z = 3", ""]
    assert content.represent() == content.text

    assert api.read_file("src/a.py", start_line=1, end_line=1).text == "y = foo(2)"
    assert api.read_file("src/a.py", start_line=-2).lines == ["z = 3", ""]


def test_create_text_file(api: FsApi, project: Project) -> None:
    result = api.create_text_file("sub/new.txt", "hello\n")
    assert "new.txt" in result
    assert (Path(project.project_root) / "sub" / "new.txt").read_text(encoding="utf-8") == "hello\n"

    result = api.create_text_file("sub/new.txt", "changed\n")
    assert "Overwrote" in result

    with pytest.raises(ValueError, match="outside the project root"):
        api.create_text_file("../outside.txt", "nope")


def test_list_dir_and_find_file(api: FsApi) -> None:
    listing = api.list_dir(".", recursive=True)
    assert "src" in listing.dirs
    assert {"src/a.py", "src/b.txt", "README.md"} <= {f.replace("\\", "/") for f in listing.files}
    assert '"dirs"' in listing.represent() and '"files"' in listing.represent()

    with pytest.raises(FileNotFoundError):
        api.list_dir("missing", recursive=False)

    assert [f.replace("\\", "/") for f in api.find_file("*.py", ".")] == ["src/a.py"]


def test_search_for_pattern(api: FsApi) -> None:
    matches = api.search_for_pattern("foo", relative_path="src")
    assert len(matches) == 3
    assert {m.source_file_path.replace("\\", "/") for m in matches.matches} == {"src/a.py", "src/b.txt"}  # type: ignore

    # restricting to code files excludes the text file; the rendering maps files to matched lines
    code_matches = api.search_for_pattern("foo", restrict_search_to_code_files=True)
    assert all(m.source_file_path.endswith("a.py") for m in code_matches.matches)  # type: ignore
    assert "foo(1)" in code_matches.represent()


def test_search_by_enclosing_symbol_refuses_a_missing_root(api: FsApi) -> None:
    with pytest.raises(FileNotFoundError, match="missing"):
        api.search_by_enclosing_symbol("foo", relative_path="missing")


def test_search_by_enclosing_symbol_groups_a_match_by_file_when_the_language_server_fails(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingRetriever:
        def __init__(self, project: Project) -> None:
            pass

        def can_analyze_file(self, relative_path: str) -> bool:
            return True

        def get_language_server(self, relative_path: str) -> None:
            raise RuntimeError("language server unavailable")

    monkeypatch.setattr("serena.symbol.LanguageServerSymbolRetriever", FailingRetriever)
    agent = MagicMock()
    agent.get_active_project_or_raise.return_value = project
    agent.get_language_backend.return_value.is_lsp.return_value = True

    hits = FsApi(agent).search_by_enclosing_symbol("foo", relative_path="src/a.py")

    assert hits.render_summary() == "Found 2 matches across 0 symbols in 1 files."
    assert "@ src/a.py [src/a.py] - 2 matches" in hits.render_symbol_list()


def test_search_by_enclosing_symbol_groups_a_match_under_its_enclosing_symbol(python_serena_agent: SerenaAgent) -> None:
    hits = FsApi(python_serena_agent).search_by_enclosing_symbol("User with ID", relative_path="test_repo/services.py")

    assert hits.render_summary() == "Found 1 matches across 1 symbols in 1 files."
    assert "@ UserService/create_user (Method) [test_repo/services.py] - 1 matches" in hits.render_symbol_list()
