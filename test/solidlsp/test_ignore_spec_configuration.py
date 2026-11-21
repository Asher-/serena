"""
tests for how a language server takes its ignore decisions from the per-path spec function of its configuration,
on servers whose process is never started.
"""

import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from pathspec import PathSpec

from serena.util.file_system import IgnoreSpecs
from solidlsp.language_servers.csharp_language_server import CSharpLanguageServer
from solidlsp.language_servers.pyright_server import PyrightServer
from solidlsp.ls_config import LanguageServerConfig, LanguageServerId
from solidlsp.ls_utils import PathUtils
from solidlsp.settings import SolidLSPSettings


def _recording_spec_of(*lines: str) -> tuple[Callable[[str], PathSpec], list[str]]:
    """:return: a per-path spec function whose spec is built from the given lines, and the list of paths it was asked about"""
    spec = PathSpec.from_lines("gitwildmatch", lines)
    asked: list[str] = []

    def spec_of(relative_path: str) -> PathSpec:
        asked.append(relative_path)
        return spec

    return spec_of, asked


def _pyright_server(tmp_path: Path, config: LanguageServerConfig) -> tuple[PyrightServer, Mock]:
    """:return: a Pyright server built by the real constructor, its low-level interface replaced so no process starts"""
    settings = SolidLSPSettings(solidlsp_dir=str(tmp_path / "global"), project_data_path=str(tmp_path / "project"))
    create_interface = Mock(return_value=Mock())
    with patch.object(PyrightServer, "_create_language_server_interface", create_interface):
        server = PyrightServer(config, str(tmp_path), settings)
    return server, create_interface


def test_a_configuration_without_a_spec_function_ignores_nothing() -> None:
    spec = LanguageServerConfig(ls_id=LanguageServerId.PYTHON).ignore_spec_of("any/path.py")

    assert not spec.match_file("any/path.py")
    assert not spec.match_file("/any/")


def test_construction_keeps_the_spec_function_and_passes_no_logger_without_tracing(tmp_path: Path) -> None:
    spec_of, asked = _recording_spec_of("generated/")

    server, create_interface = _pyright_server(tmp_path, LanguageServerConfig(ls_id=LanguageServerId.PYTHON, ignore_spec_of=spec_of))

    assert server.config.ignore_spec_of is spec_of
    assert asked == []  # constructing a server asks about no path
    assert create_interface.call_args.args == (None,)


def test_construction_passes_a_logger_when_tracing(tmp_path: Path) -> None:
    config = LanguageServerConfig(ls_id=LanguageServerId.PYTHON, trace_lsp_communication=True)

    _, create_interface = _pyright_server(tmp_path, config)

    assert callable(create_interface.call_args.args[0])


def test_is_ignored_path_asks_the_spec_function_about_the_path(tmp_path: Path) -> None:
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated" / "out.py").write_text("x = 1\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n")
    (tmp_path / "README.md").write_text("readme\n")
    (tmp_path / "venv").mkdir()
    spec_of, asked = _recording_spec_of("generated/")
    server, _ = _pyright_server(tmp_path, LanguageServerConfig(ls_id=LanguageServerId.PYTHON, ignore_spec_of=spec_of))

    assert server.is_ignored_path("generated/out.py")
    assert not server.is_ignored_path("pkg/mod.py")
    assert asked == ["generated/out.py", "pkg/mod.py"]
    # a path under an always-ignored directory name and an unsupported file are decided before the spec is asked
    assert server.is_ignored_path("venv", ignore_unsupported_files=False)
    assert server.is_ignored_path("README.md")
    assert asked == ["generated/out.py", "pkg/mod.py"]
    # a directory entry the caller already holds is decided by the same per-path spec
    (entry,) = [entry for entry in os.scandir(tmp_path) if entry.name == "generated"]
    assert server.is_ignored_path("generated", ignore_unsupported_files=False, dir_entry=entry)
    assert asked[-1] == "generated"


def _bare_csharp_server(root: Path, spec_of: Callable[[str], PathSpec]) -> tuple[CSharpLanguageServer, list[tuple[str, Any]]]:
    """:return: a C# server that was never constructed or started, and the notifications it sends"""
    sent: list[tuple[str, Any]] = []
    server = CSharpLanguageServer.__new__(CSharpLanguageServer)
    server.repository_root_path = str(root)
    server.config = LanguageServerConfig(ls_id=LanguageServerId.CSHARP, ignore_spec_of=spec_of)
    server.server = SimpleNamespace(notify=SimpleNamespace(send_notification=lambda method, params: sent.append((method, params))))
    return server, sent


def test_csharp_opens_the_solution_and_the_projects_outside_ignored_trees(tmp_path: Path) -> None:
    for name in ("src", "vendor"):
        (tmp_path / name).mkdir()
    (tmp_path / "app.sln").write_text("")
    (tmp_path / "src" / "app.csproj").write_text("<Project/>")
    (tmp_path / "vendor" / "lib.csproj").write_text("<Project/>")
    spec_of, asked = _recording_spec_of("vendor/")
    server, sent = _bare_csharp_server(tmp_path, spec_of)

    server._open_solution_and_projects()

    assert sent == [
        ("solution/open", {"solution": PathUtils.path_to_uri(str(tmp_path / "app.sln"))}),
        ("project/open", {"projects": [PathUtils.path_to_uri(str(tmp_path / "src" / "app.csproj"))]}),
    ]
    assert "vendor" in asked


def test_csharp_opens_nothing_when_every_solution_and_project_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "other.sln").write_text("")
    (tmp_path / "vendor" / "lib.csproj").write_text("<Project/>")
    spec_of, _ = _recording_spec_of("vendor/")
    server, sent = _bare_csharp_server(tmp_path, spec_of)

    server._open_solution_and_projects()

    assert sent == []


def test_a_server_configured_without_a_spec_function_ignores_no_source_path(tmp_path: Path) -> None:
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated" / "out.py").write_text("x = 1\n")
    server, _ = _pyright_server(tmp_path, LanguageServerConfig(ls_id=LanguageServerId.PYTHON))

    assert not server.is_ignored_path("generated/out.py")
    assert not server.is_ignored_path("generated", ignore_unsupported_files=False)


def test_csharp_skips_the_trees_a_projects_gitignore_excludes(tmp_path: Path) -> None:
    """The C# server, given a project's IgnoreSpecs, leaves out a tree the root .gitignore excludes."""
    for name in ("src", "samples"):
        (tmp_path / name).mkdir()
    (tmp_path / ".gitignore").write_text("samples/\n")
    (tmp_path / "src" / "app.csproj").write_text("<Project/>")
    (tmp_path / "samples" / "demo.csproj").write_text("<Project/>")
    ignore_specs = IgnoreSpecs(str(tmp_path), [".git"], read_gitignore_files=True)
    server, sent = _bare_csharp_server(tmp_path, ignore_specs.spec_of)

    server._open_solution_and_projects()

    assert sent == [("project/open", {"projects": [PathUtils.path_to_uri(str(tmp_path / "src" / "app.csproj"))]})]
