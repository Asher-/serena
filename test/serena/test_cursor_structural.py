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
from unittest.mock import MagicMock

import pytest

from serena.cursor import (
    CursorManager,
    StructuralResolution,
)
from solidlsp.structural.backends.python import PythonStructuralLanguage
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

    def test_default_registry_exposes_five_backends(self) -> None:
        registry = default_structural_backend_registry()
        assert registry.registered_languages() == frozenset({"python", "cpp", "markdown", "swift", "json"})


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
