"""Unit tests for :class:`CursorManager` structural name-path resolution.

Covers the Commit-3 routing surface:

* :class:`StructuralBackendRegistry` extension / language lookup, case
  normalization, memoization.
* :meth:`CursorManager.resolve_structural_name_path` for named paths, synthetic
  ``parent/<kind>#<index>`` paths, unsupported extensions, missing files, and
  mtime-based cache invalidation.

The tests instantiate a real :class:`~solidlsp.structural.backends.python.PythonStructuralLanguage`
because it is fast to parse and already covered by the structural backend suite;
the :class:`Project` dependency is mocked because :meth:`CursorManager.resolve_structural_name_path`
only uses ``project_root`` and ``read_file``.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from serena.cursor import (
    CursorManager,
    ReadRung,
    StructuralCursorState,
    StructuralResolution,
)
from solidlsp.structural.backends.python import PythonStructuralLanguage
from solidlsp.structural.backends.toml import TomlStructuralLanguage
from solidlsp.structural.backends.yaml import YamlStructuralLanguage
from solidlsp.structural.registry import (
    StructuralBackendRegistry,
    default_structural_backend_registry,
)

# sample source the Python structural backend walks for every test
_PYTHON_SOURCE = (
    "class Thing:\n"
    "    def method(self, x: int) -> int:\n"
    "        if x > 0:\n"
    "            return x\n"
    "        return -x\n"
    "\n"
    "\n"
    "def top_level() -> None:\n"
    "    if True:\n"
    "        pass\n"
)


@pytest.fixture
def project_with_python_file(tmp_path: Path) -> tuple[MagicMock, str, str]:
    rel_path = "sample.py"
    abs_path = tmp_path / rel_path
    abs_path.write_text(_PYTHON_SOURCE, encoding="utf-8")

    project = MagicMock()
    project.project_root = str(tmp_path)
    project.read_file = MagicMock(side_effect=lambda p: Path(tmp_path / p).read_text(encoding="utf-8"))
    return project, rel_path, str(abs_path)


@pytest.fixture
def python_only_registry() -> StructuralBackendRegistry:
    registry = StructuralBackendRegistry()
    registry.register("python", [".py", ".pyi"], PythonStructuralLanguage)
    return registry


class TestStructuralBackendRegistry:
    def test_register_then_lookup_by_language_returns_instance(self) -> None:
        registry = StructuralBackendRegistry()
        registry.register("python", [".py"], PythonStructuralLanguage)
        backend = registry.for_language("python")
        assert isinstance(backend, PythonStructuralLanguage)

    def test_for_language_is_case_insensitive(self) -> None:
        registry = StructuralBackendRegistry()
        registry.register("python", [".py"], PythonStructuralLanguage)
        assert isinstance(registry.for_language("PYTHON"), PythonStructuralLanguage)
        assert isinstance(registry.for_language("Python"), PythonStructuralLanguage)

    def test_for_relative_path_matches_extension_case_insensitively(self) -> None:
        registry = StructuralBackendRegistry()
        registry.register("python", [".py", ".pyi"], PythonStructuralLanguage)
        assert isinstance(registry.for_relative_path("src/foo.py"), PythonStructuralLanguage)
        assert isinstance(registry.for_relative_path("src/FOO.PY"), PythonStructuralLanguage)
        assert isinstance(registry.for_relative_path("foo.pyi"), PythonStructuralLanguage)

    def test_for_relative_path_returns_none_for_unregistered_extension(self) -> None:
        registry = StructuralBackendRegistry()
        registry.register("python", [".py"], PythonStructuralLanguage)
        assert registry.for_relative_path("docs/readme.md") is None
        assert registry.for_relative_path("no_extension") is None

    def test_for_language_returns_none_for_unregistered_language(self) -> None:
        registry = StructuralBackendRegistry()
        assert registry.for_language("python") is None

    def test_registered_languages_reflects_registrations(self) -> None:
        registry = StructuralBackendRegistry()
        assert registry.registered_languages() == frozenset()
        registry.register("python", [".py"], PythonStructuralLanguage)
        assert registry.registered_languages() == frozenset({"python"})

    def test_factory_is_called_once_per_language(self) -> None:
        registry = StructuralBackendRegistry()
        factory = MagicMock(return_value=PythonStructuralLanguage())
        registry.register("python", [".py"], factory)

        first = registry.for_language("python")
        second = registry.for_language("python")
        third = registry.for_relative_path("x.py")

        assert factory.call_count == 1
        assert first is second is third

    def test_register_rejects_extensions_without_dot(self) -> None:
        registry = StructuralBackendRegistry()
        with pytest.raises(ValueError, match="must start with"):
            registry.register("python", ["py"], PythonStructuralLanguage)

    def test_register_rejects_empty_language_key(self) -> None:
        registry = StructuralBackendRegistry()
        with pytest.raises(ValueError, match="non-empty"):
            registry.register("", [".py"], PythonStructuralLanguage)

    def test_default_registry_exposes_thirteen_backends(self) -> None:
        registry = default_structural_backend_registry()
        assert registry.registered_languages() == frozenset(
            {"python", "cpp", "markdown", "swift", "json", "yaml", "toml", "typescript", "go", "rust", "java", "ruby", "csharp"}
        )


class TestResolveStructuralNamePath:
    def test_resolves_named_symbol_path(
        self,
        project_with_python_file: tuple[MagicMock, str, str],
        python_only_registry: StructuralBackendRegistry,
    ) -> None:
        project, rel_path, _ = project_with_python_file
        manager = CursorManager(project, structural_registry=python_only_registry)

        resolution = manager.resolve_structural_name_path(rel_path, "Thing/method")

        assert resolution is not None
        assert isinstance(resolution, StructuralResolution)
        assert resolution.name_path == "Thing/method"
        assert resolution.kind == "method"

    def test_resolves_synthetic_unnamed_compound_path(
        self,
        project_with_python_file: tuple[MagicMock, str, str],
        python_only_registry: StructuralBackendRegistry,
    ) -> None:
        project, rel_path, _ = project_with_python_file
        manager = CursorManager(project, structural_registry=python_only_registry)

        resolution = manager.resolve_structural_name_path(rel_path, "Thing/method/if_stmt#0")

        assert resolution is not None
        assert resolution.kind == "if_stmt"
        assert resolution.name_path == "Thing/method/if_stmt#0"

    def test_resolves_top_level_synthetic_path(
        self,
        project_with_python_file: tuple[MagicMock, str, str],
        python_only_registry: StructuralBackendRegistry,
    ) -> None:
        project, rel_path, _ = project_with_python_file
        manager = CursorManager(project, structural_registry=python_only_registry)

        resolution = manager.resolve_structural_name_path(rel_path, "top_level/if_stmt#0")

        assert resolution is not None
        assert resolution.kind == "if_stmt"

    def test_returns_none_for_unknown_name_path(
        self,
        project_with_python_file: tuple[MagicMock, str, str],
        python_only_registry: StructuralBackendRegistry,
    ) -> None:
        project, rel_path, _ = project_with_python_file
        manager = CursorManager(project, structural_registry=python_only_registry)

        assert manager.resolve_structural_name_path(rel_path, "Nonexistent") is None
        assert manager.resolve_structural_name_path(rel_path, "Thing/method/if_stmt#99") is None

    def test_returns_none_for_unsupported_extension(
        self,
        project_with_python_file: tuple[MagicMock, str, str],
        python_only_registry: StructuralBackendRegistry,
    ) -> None:
        project, _, _ = project_with_python_file
        manager = CursorManager(project, structural_registry=python_only_registry)

        assert manager.resolve_structural_name_path("docs/readme.md", "Anything") is None
        project.read_file.assert_not_called()

    def test_returns_none_for_missing_file(
        self,
        python_only_registry: StructuralBackendRegistry,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project = MagicMock()
            project.project_root = tmpdir
            manager = CursorManager(project, structural_registry=python_only_registry)

            assert manager.resolve_structural_name_path("missing.py", "Whatever") is None

    def test_cache_hit_avoids_reparsing(
        self,
        project_with_python_file: tuple[MagicMock, str, str],
        python_only_registry: StructuralBackendRegistry,
    ) -> None:
        project, rel_path, _ = project_with_python_file
        manager = CursorManager(project, structural_registry=python_only_registry)

        manager.resolve_structural_name_path(rel_path, "Thing/method")
        manager.resolve_structural_name_path(rel_path, "Thing")
        manager.resolve_structural_name_path(rel_path, "top_level/if_stmt#0")

        assert project.read_file.call_count == 1

    def test_mtime_change_invalidates_cache(
        self,
        project_with_python_file: tuple[MagicMock, str, str],
        python_only_registry: StructuralBackendRegistry,
    ) -> None:
        project, rel_path, abs_path = project_with_python_file
        manager = CursorManager(project, structural_registry=python_only_registry)

        first = manager.resolve_structural_name_path(rel_path, "Thing/method")
        assert first is not None
        assert project.read_file.call_count == 1

        new_source = "class Thing:\n    def method(self) -> None:\n        pass\n\n\ndef added() -> None:\n    pass\n"
        Path(abs_path).write_text(new_source, encoding="utf-8")
        stat = os.stat(abs_path)
        os.utime(abs_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

        resolution_after = manager.resolve_structural_name_path(rel_path, "added")

        assert resolution_after is not None
        assert resolution_after.kind == "function"
        assert project.read_file.call_count == 2

    def test_returns_none_when_empty_registry(
        self,
        project_with_python_file: tuple[MagicMock, str, str],
    ) -> None:
        project, rel_path, _ = project_with_python_file
        empty_registry = StructuralBackendRegistry()
        manager = CursorManager(project, structural_registry=empty_registry)

        assert manager.resolve_structural_name_path(rel_path, "Thing") is None
        project.read_file.assert_not_called()

    def test_default_registry_used_when_none_provided(
        self,
        project_with_python_file: tuple[MagicMock, str, str],
    ) -> None:
        project, rel_path, _ = project_with_python_file
        manager = CursorManager(project)

        resolution = manager.resolve_structural_name_path(rel_path, "Thing")

        assert resolution is not None
        assert resolution.kind == "class"


class TestFormatStructuralNodeSourceRendersValue:
    """_format_structural_node_source returns the node's VALUE text, never the
    ``(mapping, key)`` tuple repr the pre-fix ``str(node)`` fallback leaked
    (spec-v2 §5.2; bug://serena/cursor-structural-scalar-include-body-leaks-node-repr).
    """

    def _manager_for(
        self,
        tmp_path: Path,
        rel_path: str,
        source: str,
        language: str,
        extensions: list[str],
        backend_cls: type,
    ) -> CursorManager:
        (tmp_path / rel_path).write_text(source, encoding="utf-8")
        project = MagicMock()
        project.project_root = str(tmp_path)
        project.read_file = MagicMock(side_effect=lambda p: Path(tmp_path / p).read_text(encoding="utf-8"))
        registry = StructuralBackendRegistry()
        registry.register(language, extensions, backend_cls)
        return CursorManager(project, structural_registry=registry)

    def test_yaml_pair_body_is_value_not_tuple(self, tmp_path: Path) -> None:
        manager = self._manager_for(
            tmp_path,
            "compose.yaml",
            "services:\n  serena:\n    image: serena:latest\n",
            "yaml",
            [".yaml", ".yml"],
            YamlStructuralLanguage,
        )
        state = StructuralCursorState(
            cursor_id="c1",
            relative_path="compose.yaml",
            name_path="services/serena/image",
            kind="pair",
            include_body=True,
        )
        assert manager._format_structural_node_source(state) == "image: serena:latest"

    def test_toml_pair_body_is_value_not_tuple(self, tmp_path: Path) -> None:
        manager = self._manager_for(
            tmp_path,
            "pyproject.toml",
            "[tool.ruff]\nline-length = 140\n",
            "toml",
            [".toml"],
            TomlStructuralLanguage,
        )
        state = StructuralCursorState(
            cursor_id="c2",
            relative_path="pyproject.toml",
            name_path="tool/ruff/line-length",
            kind="pair",
            include_body=True,
        )
        assert manager._format_structural_node_source(state) == "line-length = 140"


class TestReadRungLadder:
    """``resolve_read_rung`` is the single read-resolution ladder ``cursor_overview`` /
    ``cursor_grep`` / ``cursor_start`` share (spec-v2 §5.1/§5.3): LSP -> structural ->
    plaintext floor, and it NEVER raises -- every path resolves to a rung.
    """

    def _manager(self, tmp_path: Path) -> CursorManager:
        project = MagicMock()
        project.project_root = str(tmp_path)
        project.read_file = MagicMock(side_effect=lambda p: Path(tmp_path / p).read_text(encoding="utf-8"))
        return CursorManager(project)  # default 13-backend registry

    def test_lsp_rung_when_analyzer_claims_file(self, tmp_path: Path) -> None:
        manager = self._manager(tmp_path)
        retriever = MagicMock()
        retriever.can_analyze_file.return_value = True
        assert manager.resolve_read_rung("src/app.py", retriever=retriever) is ReadRung.LSP

    def test_structural_rung_when_backend_registered_and_no_lsp(self, tmp_path: Path) -> None:
        manager = self._manager(tmp_path)
        retriever = MagicMock()
        retriever.can_analyze_file.return_value = False
        assert manager.resolve_read_rung("compose.yaml", retriever=retriever) is ReadRung.STRUCTURAL
        assert manager.resolve_read_rung("pkg.json", retriever=retriever) is ReadRung.STRUCTURAL
        assert manager.resolve_read_rung("Cargo.toml", retriever=retriever) is ReadRung.STRUCTURAL
        # tree-sitter fallback rung (T6, spec-v2 §5.8): a file no explicit backend
        # owns but a shipped grammar covers now reads structurally, above the floor
        assert manager.resolve_read_rung("Dockerfile", retriever=retriever) is ReadRung.STRUCTURAL
        assert manager.resolve_read_rung("styles.css", retriever=retriever) is ReadRung.STRUCTURAL

    def test_plaintext_floor_when_no_lsp_and_no_backend(self, tmp_path: Path) -> None:
        manager = self._manager(tmp_path)
        retriever = MagicMock()
        retriever.can_analyze_file.return_value = False
        assert manager.resolve_read_rung("LICENSE", retriever=retriever) is ReadRung.PLAINTEXT
        assert manager.resolve_read_rung("notes.txt", retriever=retriever) is ReadRung.PLAINTEXT
        assert manager.resolve_read_rung(".gitignore", retriever=retriever) is ReadRung.PLAINTEXT
        # Dockerfile moved to the structural rung in T6 (tree-sitter fallback); the
        # floor now serves only files no grammar covers (LICENSE / .txt / dotfiles)

    def test_never_raises_and_always_returns_a_rung(self, tmp_path: Path) -> None:
        manager = self._manager(tmp_path)
        retriever = MagicMock()
        retriever.can_analyze_file.return_value = False
        for p in ("weird.unknownext", "no_ext", "a.b.c"):
            assert isinstance(manager.resolve_read_rung(p, retriever=retriever), ReadRung)


class TestStructuralOverview:
    """``structural_overview`` lists a non-LSP file's TOP-LEVEL nodes so
    ``cursor_overview`` can fall through to the structural rung instead of the
    old 'Cannot extract symbols' raise (spec-v2 §5.1/§5.3). Never raises.
    """

    def _manager(self, tmp_path: Path, rel_path: str, source: str) -> CursorManager:
        (tmp_path / rel_path).write_text(source, encoding="utf-8")
        project = MagicMock()
        project.project_root = str(tmp_path)
        project.read_file = MagicMock(side_effect=lambda p: Path(tmp_path / p).read_text(encoding="utf-8"))
        return CursorManager(project)

    def test_yaml_lists_top_level_keys_only(self, tmp_path: Path) -> None:
        manager = self._manager(
            tmp_path,
            "compose.yaml",
            "services:\n  serena:\n    image: serena:latest\nversion: '3'\n",
        )
        top = manager.structural_overview("compose.yaml")
        names = [name for name, _kind in top]
        assert "services" in names
        assert "version" in names
        # nested keys are NOT top level
        assert "services/serena" not in names

    def test_empty_for_unregistered_extension(self, tmp_path: Path) -> None:
        manager = self._manager(tmp_path, "LICENSE", "All rights reserved.\n")
        assert manager.structural_overview("LICENSE") == []

    def test_empty_for_missing_file(self, tmp_path: Path) -> None:
        project = MagicMock()
        project.project_root = str(tmp_path)
        project.read_file = MagicMock(side_effect=lambda p: Path(tmp_path / p).read_text(encoding="utf-8"))
        manager = CursorManager(project)
        assert manager.structural_overview("nope.yaml") == []


class TestFindPatternSurfacesNonSymbolHits:
    """``find_pattern_with_enclosing_symbols`` surfaces matches with no enclosing
    LSP symbol as file-level ``(relative_path, [display])`` blocks carrying the
    matched line + number, instead of dropping them to a count (spec-v2
    §5.1/§5.3). Exercises the REAL manager method end-to-end against a real
    non-LSP file -- the live counterpart to the mocked tool-layer tests in
    test_cursor.py::TestCursorGrepSurfacesNonSymbolHits.
    """

    def test_non_symbol_match_surfaced_with_content_not_counted(self, tmp_path: Path) -> None:
        (tmp_path / "compose.yaml").write_text(
            "services:\n  serena:\n    image: serena:latest\n", encoding="utf-8"
        )
        project = MagicMock()
        project.project_root = str(tmp_path)
        project.read_file = MagicMock(side_effect=lambda p: Path(tmp_path / p).read_text(encoding="utf-8"))
        # No language server serves a .yaml file, so the read ladder resolves it
        # off the LSP rung and the match routes through the non-symbol surfacing
        # path -- exactly as in production. Patch ONLY the LSP retriever (mirrors
        # test_cursor.py); the manager method, structural registry, file I/O, and
        # _record_unsymboled all run for real against the on-disk file.
        with patch("serena.cursor.LanguageServerSymbolRetriever") as retriever_cls:
            retriever_cls.return_value.can_analyze_file.return_value = False
            manager = CursorManager(project)
            groups, unsymboled = manager.find_pattern_with_enclosing_symbols(
                substring_pattern=r"serena:latest",
                relative_path="compose.yaml",
                restrict_to_code_files=False,
            )
        # a non-LSP file has no enclosing symbol to anchor a cursor ...
        assert groups == []
        # ... but the match is SURFACED as a file-level block with its matched
        # line content -- not reduced to a bare count, not dropped
        assert len(unsymboled) == 1
        path, displays = unsymboled[0]
        assert path == "compose.yaml"
        assert any("serena:latest" in d for d in displays), displays
