"""Cross-language integration tests for cursor-layer container-member editing.

Verifies the wiring introduced in Direction B's cursor layer:

* :meth:`CursorManager.start_cursor` falls through to structural resolution
  when the language server cannot locate the name path, producing a
  :class:`StructuralCursorState` that identifies the container member.
* :meth:`CursorManager.apply_container_edit` dispatches to the correct
  ``container_insert_member`` / ``container_replace_member`` method on each
  backend, serializes the result, and writes it back to disk.
* The three edit tools (:class:`CursorReplaceBodyTool`,
  :class:`CursorInsertBeforeTool`, :class:`CursorInsertAfterTool`) take the
  structural branch when the cursor is a structural cursor and produce the
  expected on-disk result.

The tests bypass :class:`SerenaAgent` for JSON / TOML / YAML because those
backends are structural-only and the agent's LSP lifecycle is unrelated to
the dispatch being tested. The Python case uses a stubbed
:class:`LanguageServerSymbolRetriever` so ``find_unique`` always raises — the
same LSP-miss condition a real agent would experience for a dict-member path
like ``FOO/["members"]``. This keeps the test hermetic and fast while still
exercising the production code path (LSP try, structural fallback).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from serena.cursor import (
    CursorManager,
    StructuralCursorState,
)
from serena.tools.cursor_tools import (
    CursorConfigureTool,
    CursorInsertAfterTool,
    CursorInsertAtEndTool,
    CursorInsertAtStartTool,
    CursorInsertBeforeTool,
    CursorRemoveMemberTool,
    CursorReplaceBodyTool,
)


class _StubRetriever:
    """Minimal ``LanguageServerSymbolRetriever`` stand-in that always misses.

    Forces :meth:`CursorManager.start_cursor` into its structural fallback
    path by raising ``ValueError`` on every ``find_unique`` call — the same
    behavior the real retriever exhibits for container-member paths that no
    language server surfaces as symbols (JSON/TOML/YAML have no LSP, and
    Python's LSP doesn't walk into dict literals).
    """

    def find_unique(
        self,
        name_path_pattern: str,
        **_kwargs: Any,
    ) -> Any:
        raise ValueError(f"No symbol matching {name_path_pattern!r} found")


class _ManagerWithStubRetriever(CursorManager):
    """Subclass that hard-wires the retriever stub so ``_retriever`` returns it.

    ``CursorManager._retriever`` is a ``@property`` on the base class that
    constructs a :class:`LanguageServerSymbolRetriever` on every access. For
    tests that want to avoid the real retriever entirely we override the
    property to return a fixed stub.
    """

    def __init__(self, project: Any, retriever_stub: Any) -> None:
        super().__init__(project=project)
        self._retriever_stub = retriever_stub

    @property  # type: ignore[override]
    def _retriever(self) -> Any:
        return self._retriever_stub


def _make_manager_via_subclass(tmp_path: Path) -> CursorManager:
    """Return a manager whose ``_retriever`` is a stub that always misses."""
    project = MagicMock()
    project.project_root = str(tmp_path)
    project.read_file = lambda p: (tmp_path / p).read_text(encoding="utf-8")
    project.project_config = MagicMock()
    project.project_config.encoding = "utf-8"
    project.line_ending = MagicMock()
    project.line_ending.newline_str = None
    return _ManagerWithStubRetriever(project=project, retriever_stub=_StubRetriever())


# --- Python -----------------------------------------------------------------


class TestPythonContainerEdit:
    """Structural cursor + container-member edits on a Python dict literal.

    Uses the canonical shape from the strongai ``Instantiation`` repro:
    a top-level dict assignment with a ``"members"`` key whose value is a
    nested dict. The path grammar mirrors the predecessor's walk_nodes:
    ``FOO/["members"]`` addresses the outer member, and the replace/insert
    target is ``FOO/["members"]/["existing"]``.
    """

    _SOURCE = 'FOO = {\n    "members": {\n        "existing": 1,\n    },\n}\n'

    def test_start_cursor_falls_through_to_structural(self, tmp_path: Path) -> None:
        (tmp_path / "sample.py").write_text(self._SOURCE, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)

        cid, state = manager.start_cursor(
            name_path='FOO/["members"]',
            relative_path="sample.py",
        )

        assert isinstance(state, StructuralCursorState)
        assert state.cursor_id == cid
        assert state.relative_path == "sample.py"
        assert state.name_path == 'FOO/["members"]'

    def test_container_replace_member_keeps_key(self, tmp_path: Path) -> None:
        (tmp_path / "sample.py").write_text(self._SOURCE, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)

        cid, _ = manager.start_cursor(
            name_path='FOO/["members"]/["existing"]',
            relative_path="sample.py",
        )
        before, after = manager.apply_container_edit(cid, "replace", "42")

        assert '"existing": 42' in after
        assert '"existing": 1' not in after
        # the file was written
        assert (tmp_path / "sample.py").read_text(encoding="utf-8") == after
        assert before != after

    def test_container_insert_after_appends_member(self, tmp_path: Path) -> None:
        (tmp_path / "sample.py").write_text(self._SOURCE, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)

        cid, _ = manager.start_cursor(
            name_path='FOO/["members"]/["existing"]',
            relative_path="sample.py",
        )
        _before, after = manager.apply_container_edit(
            cid,
            "insert_after",
            '"new_field": 2',
        )

        assert '"existing": 1' in after
        assert '"new_field": 2' in after
        # "new_field" must appear after "existing" on its own line
        existing_line = next(i for i, line in enumerate(after.splitlines()) if '"existing"' in line)
        new_field_line = next(i for i, line in enumerate(after.splitlines()) if '"new_field"' in line)
        assert new_field_line == existing_line + 1


# --- JSON -------------------------------------------------------------------


class TestJsonContainerEdit:
    """Structural cursor + container-member edits on a JSON object."""

    _SOURCE = '{\n    "members": {\n        "existing": 1\n    }\n}\n'

    def test_start_cursor_resolves_structural_path(self, tmp_path: Path) -> None:
        (tmp_path / "doc.json").write_text(self._SOURCE, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)

        cid, state = manager.start_cursor(
            name_path="members/existing",
            relative_path="doc.json",
        )

        assert isinstance(state, StructuralCursorState)
        assert state.cursor_id == cid
        assert state.relative_path == "doc.json"
        assert state.name_path == "members/existing"

    def test_container_replace_member(self, tmp_path: Path) -> None:
        (tmp_path / "doc.json").write_text(self._SOURCE, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)

        cid, _ = manager.start_cursor(
            name_path="members/existing",
            relative_path="doc.json",
        )
        _before, after = manager.apply_container_edit(cid, "replace", "42")

        assert '"existing": 42' in after
        assert '"existing": 1' not in after
        assert (tmp_path / "doc.json").read_text(encoding="utf-8") == after

    def test_container_insert_after(self, tmp_path: Path) -> None:
        (tmp_path / "doc.json").write_text(self._SOURCE, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)

        cid, _ = manager.start_cursor(
            name_path="members/existing",
            relative_path="doc.json",
        )
        _before, after = manager.apply_container_edit(
            cid,
            "insert_after",
            '"new_field": 2',
        )

        assert '"existing": 1' in after
        assert '"new_field": 2' in after
        existing_line = next(i for i, line in enumerate(after.splitlines()) if '"existing"' in line)
        new_field_line = next(i for i, line in enumerate(after.splitlines()) if '"new_field"' in line)
        assert new_field_line == existing_line + 1


# --- TOML -------------------------------------------------------------------


class TestTomlContainerEdit:
    """Structural cursor + container-member edits on a TOML document."""

    _SOURCE = "[members]\nexisting = 1\n"

    def test_start_cursor_resolves_structural_path(self, tmp_path: Path) -> None:
        (tmp_path / "config.toml").write_text(self._SOURCE, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)

        cid, state = manager.start_cursor(
            name_path="members/existing",
            relative_path="config.toml",
        )

        assert isinstance(state, StructuralCursorState)
        assert state.cursor_id == cid
        assert state.relative_path == "config.toml"

    def test_container_replace_member_preserves_key(self, tmp_path: Path) -> None:
        (tmp_path / "config.toml").write_text(self._SOURCE, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)

        cid, _ = manager.start_cursor(
            name_path="members/existing",
            relative_path="config.toml",
        )
        _before, after = manager.apply_container_edit(cid, "replace", "42")

        assert "existing = 42" in after
        assert "existing = 1" not in after
        assert (tmp_path / "config.toml").read_text(encoding="utf-8") == after

    def test_container_insert_after(self, tmp_path: Path) -> None:
        (tmp_path / "config.toml").write_text(self._SOURCE, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)

        cid, _ = manager.start_cursor(
            name_path="members/existing",
            relative_path="config.toml",
        )
        _before, after = manager.apply_container_edit(
            cid,
            "insert_after",
            "new_field = 2",
        )

        assert "existing = 1" in after
        assert "new_field = 2" in after
        existing_line = next(i for i, line in enumerate(after.splitlines()) if "existing =" in line)
        new_field_line = next(i for i, line in enumerate(after.splitlines()) if "new_field =" in line)
        assert new_field_line == existing_line + 1


# --- YAML -------------------------------------------------------------------


class TestYamlContainerEdit:
    """Structural cursor + container-member edits on a YAML mapping."""

    _SOURCE = "members:\n  existing: 1\n"

    def test_start_cursor_resolves_structural_path(self, tmp_path: Path) -> None:
        (tmp_path / "data.yaml").write_text(self._SOURCE, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)

        cid, state = manager.start_cursor(
            name_path="members/existing",
            relative_path="data.yaml",
        )

        assert isinstance(state, StructuralCursorState)
        assert state.cursor_id == cid
        assert state.relative_path == "data.yaml"

    def test_container_replace_member_preserves_key(self, tmp_path: Path) -> None:
        (tmp_path / "data.yaml").write_text(self._SOURCE, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)

        cid, _ = manager.start_cursor(
            name_path="members/existing",
            relative_path="data.yaml",
        )
        _before, after = manager.apply_container_edit(cid, "replace", "42")

        assert "existing: 42" in after
        assert "existing: 1" not in after
        assert (tmp_path / "data.yaml").read_text(encoding="utf-8") == after

    def test_container_insert_after(self, tmp_path: Path) -> None:
        (tmp_path / "data.yaml").write_text(self._SOURCE, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)

        cid, _ = manager.start_cursor(
            name_path="members/existing",
            relative_path="data.yaml",
        )
        _before, after = manager.apply_container_edit(
            cid,
            "insert_after",
            "new_field: 2",
        )

        assert "existing: 1" in after
        assert "new_field: 2" in after
        existing_line = next(i for i, line in enumerate(after.splitlines()) if "existing" in line)
        new_field_line = next(i for i, line in enumerate(after.splitlines()) if "new_field" in line)
        assert new_field_line == existing_line + 1


# --- tool dispatch ----------------------------------------------------------


class _ToolHarness:
    """Minimal agent stand-in so the cursor edit tools can be invoked directly.

    The three cursor edit tools reach into ``self.agent.get_cursor_manager()``
    and ``self.project``; we provide exactly those methods so each tool's
    structural-cursor branch can be exercised without standing up the full
    :class:`SerenaAgent`. The non-structural branch is unchanged and already
    covered by the existing cursor edit tests.
    """

    def __init__(self, manager: CursorManager, project: Any) -> None:
        self._manager = manager
        self._project = project

    # mimic Tool.apply binding expectations: `self.agent` and `self.project`
    @property
    def agent(self) -> Any:
        harness = self

        class _Agent:
            def get_cursor_manager(self_inner: Any) -> CursorManager:
                return harness._manager

        return _Agent()

    @property
    def project(self) -> Any:
        return self._project


def _bind_tool(tool_cls: type, harness: _ToolHarness) -> Any:
    """Instantiate ``tool_cls`` without the real ``Tool.__init__`` so we can dispatch.

    The cursor edit tools only use ``self.agent.get_cursor_manager()`` in
    the structural branch, so replacing the instance's ``agent`` / ``project``
    attributes with the harness is sufficient.
    """
    tool = tool_cls.__new__(tool_cls)  # type: ignore[call-overload]
    tool.__dict__["agent"] = harness.agent
    tool.__dict__["project"] = harness.project
    return tool


class TestCursorEditToolsStructuralBranch:
    """End-to-end tool dispatch for all four backends.

    Each test invokes the ``apply`` method of a cursor edit tool on a
    structural cursor and asserts the on-disk result. This closes the loop
    from MCP tool surface through :class:`CursorManager.apply_container_edit`
    down to the backend method.
    """

    @pytest.fixture
    def python_manager(self, tmp_path: Path) -> tuple[CursorManager, Any, Path]:
        source = 'FOO = {\n    "members": {\n        "existing": 1,\n    },\n}\n'
        file_path = tmp_path / "sample.py"
        file_path.write_text(source, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)
        project = manager._project  # type: ignore[attr-defined]
        return manager, project, file_path

    def test_replace_body_tool_dispatches_to_structural(
        self,
        python_manager: tuple[CursorManager, Any, Path],
    ) -> None:
        manager, project, file_path = python_manager
        manager.start_cursor(
            name_path='FOO/["members"]/["existing"]',
            relative_path="sample.py",
            cursor_id="c1",
        )
        harness = _ToolHarness(manager, project)
        tool = _bind_tool(CursorReplaceBodyTool, harness)

        result = tool.apply(cursor_id="c1", body="42", expect_version="*")

        assert "SUCCESS" in result or "Diff:" in result
        content = file_path.read_text(encoding="utf-8")
        assert '"existing": 42' in content
        assert '"existing": 1' not in content

    def test_insert_after_tool_dispatches_to_structural(
        self,
        python_manager: tuple[CursorManager, Any, Path],
    ) -> None:
        manager, project, file_path = python_manager
        manager.start_cursor(
            name_path='FOO/["members"]/["existing"]',
            relative_path="sample.py",
            cursor_id="c1",
        )
        harness = _ToolHarness(manager, project)
        tool = _bind_tool(CursorInsertAfterTool, harness)

        tool.apply(cursor_id="c1", body='"new_field": 2', expect_version="*")

        content = file_path.read_text(encoding="utf-8")
        assert '"existing": 1' in content
        assert '"new_field": 2' in content

    def test_insert_before_tool_dispatches_to_structural(
        self,
        python_manager: tuple[CursorManager, Any, Path],
    ) -> None:
        manager, project, file_path = python_manager
        manager.start_cursor(
            name_path='FOO/["members"]/["existing"]',
            relative_path="sample.py",
            cursor_id="c1",
        )
        harness = _ToolHarness(manager, project)
        tool = _bind_tool(CursorInsertBeforeTool, harness)

        tool.apply(cursor_id="c1", body='"new_field": 2', expect_version="*")

        content = file_path.read_text(encoding="utf-8")
        assert '"existing": 1' in content
        assert '"new_field": 2' in content
        existing_line = next(i for i, line in enumerate(content.splitlines()) if '"existing"' in line)
        new_field_line = next(i for i, line in enumerate(content.splitlines()) if '"new_field"' in line)
        assert new_field_line == existing_line - 1


# --- canonical strongai-shaped repro ----------------------------------------


class TestStrongaiRepro:
    """Canonical Instantiation-dict repro.

    Mirrors the shape of ``strongai/code-graph/core/schema.py:79-88`` — a
    top-level ``Instantiation`` dict with a ``"members"`` key whose value is
    a nested dict of fields. The acceptance criterion is: one field is
    added, no other fields touched, formatting preserved.
    """

    _SOURCE = (
        'Instantiation = {\n    "members": {\n        "pid": "PID",\n        "aspect": "Aspect",\n        "value": "Any",\n    },\n}\n'
    )

    def test_insert_after_preserves_surroundings(self, tmp_path: Path) -> None:
        (tmp_path / "schema.py").write_text(self._SOURCE, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)

        cid, _ = manager.start_cursor(
            name_path='Instantiation/["members"]/["value"]',
            relative_path="schema.py",
        )
        _before, after = manager.apply_container_edit(
            cid,
            "insert_after",
            '"provenance": "str"',
        )

        # new field was added
        assert '"provenance": "str"' in after
        # existing fields untouched
        assert '"pid": "PID"' in after
        assert '"aspect": "Aspect"' in after
        assert '"value": "Any"' in after
        # structural framing preserved
        assert "Instantiation = {" in after
        assert '"members":' in after
        # order: provenance after value
        value_line = next(i for i, line in enumerate(after.splitlines()) if '"value"' in line)
        prov_line = next(i for i, line in enumerate(after.splitlines()) if '"provenance"' in line)
        assert prov_line == value_line + 1


class TestContainerRemoveAndAnchored:
    """Remove, insert_start, insert_end at the ``apply_container_edit`` layer.

    These three operations round-trip through the same backend plumbing as
    replace / insert_before / insert_after, but until now only the first
    three were exercised end-to-end from the cursor layer. Each backend
    gets one test per new operation — the backend unit tests already
    cover deeper permutations.
    """

    # reusable sources per backend — each has a two-member container so
    # insert_start has a neighbor to land before and insert_end has one
    # to land after
    _PYTHON = 'FOO = {\n    "members": {\n        "alpha": 1,\n        "beta": 2,\n    },\n}\n'
    _JSON = '{\n    "members": {\n        "alpha": 1,\n        "beta": 2\n    }\n}\n'
    _TOML = "[members]\nalpha = 1\nbeta = 2\n"
    _YAML = "members:\n  alpha: 1\n  beta: 2\n"

    # --- python ---------------------------------------------------------

    def test_python_remove(self, tmp_path: Path) -> None:
        (tmp_path / "s.py").write_text(self._PYTHON, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)
        cid, _ = manager.start_cursor(
            name_path='FOO/["members"]/["alpha"]',
            relative_path="s.py",
        )

        _before, after = manager.apply_container_edit(cid, "remove", "")

        assert '"alpha"' not in after
        assert '"beta": 2' in after
        assert (tmp_path / "s.py").read_text(encoding="utf-8") == after

    def test_python_insert_start_on_container(self, tmp_path: Path) -> None:
        (tmp_path / "s.py").write_text(self._PYTHON, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)
        cid, _ = manager.start_cursor(
            name_path='FOO/["members"]',
            relative_path="s.py",
        )

        _before, after = manager.apply_container_edit(cid, "insert_start", '"zero": 0')

        lines = after.splitlines()
        zero = next(i for i, ln in enumerate(lines) if '"zero"' in ln)
        alpha = next(i for i, ln in enumerate(lines) if '"alpha"' in ln)
        assert zero < alpha

    def test_python_insert_end_on_container(self, tmp_path: Path) -> None:
        (tmp_path / "s.py").write_text(self._PYTHON, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)
        cid, _ = manager.start_cursor(
            name_path='FOO/["members"]',
            relative_path="s.py",
        )

        _before, after = manager.apply_container_edit(cid, "insert_end", '"omega": 99')

        lines = after.splitlines()
        beta = next(i for i, ln in enumerate(lines) if '"beta"' in ln)
        omega = next(i for i, ln in enumerate(lines) if '"omega"' in ln)
        assert beta < omega

    # --- json -----------------------------------------------------------

    def test_json_remove(self, tmp_path: Path) -> None:
        (tmp_path / "d.json").write_text(self._JSON, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)
        cid, _ = manager.start_cursor(
            name_path="members/alpha",
            relative_path="d.json",
        )

        _before, after = manager.apply_container_edit(cid, "remove", "")

        assert "alpha" not in after
        assert '"beta": 2' in after

    def test_json_insert_start_on_container(self, tmp_path: Path) -> None:
        (tmp_path / "d.json").write_text(self._JSON, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)
        cid, _ = manager.start_cursor(
            name_path="members",
            relative_path="d.json",
        )

        _before, after = manager.apply_container_edit(cid, "insert_start", '"zero": 0')

        lines = after.splitlines()
        zero = next(i for i, ln in enumerate(lines) if '"zero"' in ln)
        alpha = next(i for i, ln in enumerate(lines) if '"alpha"' in ln)
        assert zero < alpha

    def test_json_insert_end_on_container(self, tmp_path: Path) -> None:
        (tmp_path / "d.json").write_text(self._JSON, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)
        cid, _ = manager.start_cursor(
            name_path="members",
            relative_path="d.json",
        )

        _before, after = manager.apply_container_edit(cid, "insert_end", '"omega": 99')

        lines = after.splitlines()
        beta = next(i for i, ln in enumerate(lines) if '"beta"' in ln)
        omega = next(i for i, ln in enumerate(lines) if '"omega"' in ln)
        assert beta < omega

    # --- toml -----------------------------------------------------------

    def test_toml_remove(self, tmp_path: Path) -> None:
        (tmp_path / "c.toml").write_text(self._TOML, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)
        cid, _ = manager.start_cursor(
            name_path="members/alpha",
            relative_path="c.toml",
        )

        _before, after = manager.apply_container_edit(cid, "remove", "")

        assert "alpha" not in after
        assert "beta = 2" in after

    def test_toml_insert_start_on_container(self, tmp_path: Path) -> None:
        (tmp_path / "c.toml").write_text(self._TOML, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)
        cid, _ = manager.start_cursor(
            name_path="members",
            relative_path="c.toml",
        )

        _before, after = manager.apply_container_edit(cid, "insert_start", "zero = 0")

        lines = after.splitlines()
        zero = next(i for i, ln in enumerate(lines) if "zero" in ln)
        alpha = next(i for i, ln in enumerate(lines) if "alpha" in ln)
        assert zero < alpha

    def test_toml_insert_end_on_container(self, tmp_path: Path) -> None:
        (tmp_path / "c.toml").write_text(self._TOML, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)
        cid, _ = manager.start_cursor(
            name_path="members",
            relative_path="c.toml",
        )

        _before, after = manager.apply_container_edit(cid, "insert_end", "omega = 99")

        lines = after.splitlines()
        beta = next(i for i, ln in enumerate(lines) if "beta" in ln)
        omega = next(i for i, ln in enumerate(lines) if "omega" in ln)
        assert beta < omega

    # --- yaml -----------------------------------------------------------

    def test_yaml_remove(self, tmp_path: Path) -> None:
        (tmp_path / "d.yaml").write_text(self._YAML, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)
        cid, _ = manager.start_cursor(
            name_path="members/alpha",
            relative_path="d.yaml",
        )

        _before, after = manager.apply_container_edit(cid, "remove", "")

        assert "alpha" not in after
        assert "beta: 2" in after

    def test_yaml_insert_start_on_container(self, tmp_path: Path) -> None:
        (tmp_path / "d.yaml").write_text(self._YAML, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)
        cid, _ = manager.start_cursor(
            name_path="members",
            relative_path="d.yaml",
        )

        _before, after = manager.apply_container_edit(cid, "insert_start", "zero: 0")

        lines = after.splitlines()
        zero = next(i for i, ln in enumerate(lines) if "zero" in ln)
        alpha = next(i for i, ln in enumerate(lines) if "alpha" in ln)
        assert zero < alpha

    def test_yaml_insert_end_on_container(self, tmp_path: Path) -> None:
        (tmp_path / "d.yaml").write_text(self._YAML, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)
        cid, _ = manager.start_cursor(
            name_path="members",
            relative_path="d.yaml",
        )

        _before, after = manager.apply_container_edit(cid, "insert_end", "omega: 99")

        lines = after.splitlines()
        beta = next(i for i, ln in enumerate(lines) if "beta" in ln)
        omega = next(i for i, ln in enumerate(lines) if "omega" in ln)
        assert beta < omega


class TestApplyContainerEditValidation:
    """Dispatcher-level validation of the expanded operation set."""

    _SOURCE = 'FOO = {\n    "members": {\n        "alpha": 1,\n    },\n}\n'

    def test_unknown_operation_raises(self, tmp_path: Path) -> None:
        (tmp_path / "s.py").write_text(self._SOURCE, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)
        cid, _ = manager.start_cursor(
            name_path='FOO/["members"]/["alpha"]',
            relative_path="s.py",
        )

        with pytest.raises(ValueError, match="unknown container edit operation"):
            manager.apply_container_edit(cid, "wipe", "")

    def test_insert_start_on_python_scalar_member_raises_friendly_error(
        self,
        tmp_path: Path,
    ) -> None:
        # Python scalar-valued dict member: cursor.kind == "container_member"
        (tmp_path / "s.py").write_text(self._SOURCE, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)
        cid, state = manager.start_cursor(
            name_path='FOO/["members"]/["alpha"]',
            relative_path="s.py",
        )
        assert isinstance(state, StructuralCursorState)
        assert state.kind == "container_member"

        with pytest.raises(ValueError) as excinfo:
            manager.apply_container_edit(cid, "insert_start", '"new": 1')

        message = str(excinfo.value)
        # the friendly message names the op, the cursor, the scalar kind,
        # and offers the parent path as a retry hint
        assert "insert_start" in message
        assert 'FOO/["members"]/["alpha"]' in message
        assert "scalar" in message
        assert "cursor_start on 'FOO/[\"members\"]'" in message

    def test_insert_end_on_json_array_scalar_raises_friendly_error(
        self,
        tmp_path: Path,
    ) -> None:
        # JSON array-item scalar: cursor.kind == "string"
        (tmp_path / "d.json").write_text(
            '{"items": ["first", "second"]}\n',
            encoding="utf-8",
        )
        manager = _make_manager_via_subclass(tmp_path)
        cid, state = manager.start_cursor(
            name_path="items/[0]",
            relative_path="d.json",
        )
        assert isinstance(state, StructuralCursorState)
        assert state.kind == "string"

        with pytest.raises(ValueError, match="insert_end requires a cursor positioned on a container"):
            manager.apply_container_edit(cid, "insert_end", '"third"')

    def test_member_anchored_op_on_top_level_raises_friendly_error(
        self,
        tmp_path: Path,
    ) -> None:
        # Python top-level assignment: path has no "/" separator, so no parent container
        (tmp_path / "s.py").write_text(self._SOURCE, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)
        cid, state = manager.start_cursor(
            name_path="FOO",
            relative_path="s.py",
        )
        assert isinstance(state, StructuralCursorState)

        with pytest.raises(ValueError) as excinfo:
            manager.apply_container_edit(cid, "insert_before", '"x": 1')

        message = str(excinfo.value)
        assert "insert_before" in message
        assert "top-level path" in message
        assert "'FOO'" in message
        assert "no parent container" in message

    def test_validator_is_additive_normal_dispatch_still_works(self, tmp_path: Path) -> None:
        # pre-check should not fire when the cursor is on a container-valued member
        (tmp_path / "s.py").write_text(self._SOURCE, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)
        cid, state = manager.start_cursor(
            name_path='FOO/["members"]',
            relative_path="s.py",
        )
        assert isinstance(state, StructuralCursorState)
        # container-valued member: the recursion re-yields with kind "container",
        # overwriting the earlier "container_member" entry in the cache
        assert state.kind == "container"

        before, after = manager.apply_container_edit(cid, "insert_end", '"zeta": 99')
        assert before != after
        assert '"zeta"' in after


class TestStructuralNeighbors:
    """The ``_resolve_structural_neighbors`` helper and its view integration."""

    _SOURCE = 'FOO = {\n    "members": {\n        "alpha": 1,\n        "beta": 2,\n        "gamma": 3,\n    },\n}\n'

    def test_container_cursor_yields_direct_children(self, tmp_path: Path) -> None:
        (tmp_path / "s.py").write_text(self._SOURCE, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)
        cid, state = manager.start_cursor(
            name_path='FOO/["members"]',
            relative_path="s.py",
        )

        assert isinstance(state, StructuralCursorState)
        neighbors = manager._resolve_structural_neighbors(state)  # type: ignore[attr-defined]
        names = {n.name for n in neighbors}
        assert names == {
            'FOO/["members"]/["alpha"]',
            'FOO/["members"]/["beta"]',
            'FOO/["members"]/["gamma"]',
        }
        _ = cid  # silence unused warning; cursor lifetime bound by manager

    def test_leaf_cursor_yields_no_neighbors(self, tmp_path: Path) -> None:
        (tmp_path / "s.py").write_text(self._SOURCE, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)
        _cid, state = manager.start_cursor(
            name_path='FOO/["members"]/["alpha"]',
            relative_path="s.py",
        )

        assert isinstance(state, StructuralCursorState)
        neighbors = manager._resolve_structural_neighbors(state)  # type: ignore[attr-defined]
        assert neighbors == []

    def test_format_cursor_view_lists_members(self, tmp_path: Path) -> None:
        (tmp_path / "s.py").write_text(self._SOURCE, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)
        cid, _ = manager.start_cursor(
            name_path='FOO/["members"]',
            relative_path="s.py",
        )

        view = manager.format_cursor_view(cid)
        # contains collapses to an inline list under the ``contains v`` arrow header
        assert "contains v" in view
        assert '["alpha"]' in view
        assert '["beta"]' in view
        assert '["gamma"]' in view


class TestStructuralNeighborSplitter:
    """Name-path segmenter respects bracket depth so keys with ``/`` survive."""

    def test_splits_plain_path(self) -> None:
        from serena.cursor import _split_name_path_segments

        assert _split_name_path_segments("a/b/c") == ["a", "b", "c"]

    def test_keeps_bracketed_slash_together(self) -> None:
        from serena.cursor import _split_name_path_segments

        # a key literally containing "/" in Python dict form: FOO/["a/b"]
        assert _split_name_path_segments('FOO/["a/b"]') == ["FOO", '["a/b"]']

    def test_nested_brackets(self) -> None:
        from serena.cursor import _split_name_path_segments

        assert _split_name_path_segments('FOO/[7]/["x"]') == ["FOO", "[7]", '["x"]']


class TestNewToolSurface:
    """End-to-end tool dispatch for CursorInsertAtStart / AtEnd / RemoveMember."""

    _SOURCE = 'FOO = {\n    "members": {\n        "alpha": 1,\n        "beta": 2,\n    },\n}\n'

    @pytest.fixture
    def py_state(self, tmp_path: Path) -> tuple[CursorManager, Any, Path]:
        file_path = tmp_path / "s.py"
        file_path.write_text(self._SOURCE, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)
        project = manager._project  # type: ignore[attr-defined]
        return manager, project, file_path

    def test_insert_at_start_tool(
        self,
        py_state: tuple[CursorManager, Any, Path],
    ) -> None:
        manager, project, file_path = py_state
        manager.start_cursor(
            name_path='FOO/["members"]',
            relative_path="s.py",
            cursor_id="c1",
        )
        tool = _bind_tool(CursorInsertAtStartTool, _ToolHarness(manager, project))

        tool.apply(cursor_id="c1", body='"zero": 0', expect_version="*")

        content = file_path.read_text(encoding="utf-8")
        lines = content.splitlines()
        zero = next(i for i, ln in enumerate(lines) if '"zero"' in ln)
        alpha = next(i for i, ln in enumerate(lines) if '"alpha"' in ln)
        assert zero < alpha

    def test_insert_at_end_tool(
        self,
        py_state: tuple[CursorManager, Any, Path],
    ) -> None:
        manager, project, file_path = py_state
        manager.start_cursor(
            name_path='FOO/["members"]',
            relative_path="s.py",
            cursor_id="c1",
        )
        tool = _bind_tool(CursorInsertAtEndTool, _ToolHarness(manager, project))

        tool.apply(cursor_id="c1", body='"omega": 99', expect_version="*")

        content = file_path.read_text(encoding="utf-8")
        lines = content.splitlines()
        beta = next(i for i, ln in enumerate(lines) if '"beta"' in ln)
        omega = next(i for i, ln in enumerate(lines) if '"omega"' in ln)
        assert beta < omega

    def test_remove_member_tool(
        self,
        py_state: tuple[CursorManager, Any, Path],
    ) -> None:
        manager, project, file_path = py_state
        manager.start_cursor(
            name_path='FOO/["members"]/["alpha"]',
            relative_path="s.py",
            cursor_id="c1",
        )
        tool = _bind_tool(CursorRemoveMemberTool, _ToolHarness(manager, project))

        tool.apply(cursor_id="c1", expect_version="*")

        content = file_path.read_text(encoding="utf-8")
        assert "alpha" not in content
        assert '"beta": 2' in content


class TestStructuralConfigure:
    """``CursorConfigureTool.apply`` on a structural cursor toggles ``include_body``."""

    _SOURCE = 'FOO = {\n    "members": {\n        "alpha": 1,\n    },\n}\n'

    def test_include_body_false_by_default_omits_body_block(self, tmp_path: Path) -> None:
        (tmp_path / "s.py").write_text(self._SOURCE, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)
        cid, state = manager.start_cursor(
            name_path='FOO/["members"]/["alpha"]',
            relative_path="s.py",
        )

        assert isinstance(state, StructuralCursorState)
        assert state.include_body is False
        view = manager.format_cursor_view(cid)
        assert "--- body ---" not in view

    def test_configure_toggles_include_body_on_structural_cursor(self, tmp_path: Path) -> None:
        (tmp_path / "s.py").write_text(self._SOURCE, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)
        cid, state = manager.start_cursor(
            name_path='FOO/["members"]/["alpha"]',
            relative_path="s.py",
        )

        assert isinstance(state, StructuralCursorState)

        project = manager._project  # type: ignore[attr-defined]
        tool = _bind_tool(CursorConfigureTool, _ToolHarness(manager, project))

        # flipping include_body writes through to the dataclass field
        view_on = tool.apply(cursor_id=cid, include_body=True)
        assert state.include_body
        assert "--- body ---" in view_on
        assert "--- end body ---" in view_on

        # flipping it back off also writes through
        view_off = tool.apply(cursor_id=cid, include_body=False)
        assert not state.include_body
        assert "--- body ---" not in view_off

    def test_configure_ignores_edge_types_for_structural_cursor(self, tmp_path: Path) -> None:
        (tmp_path / "s.py").write_text(self._SOURCE, encoding="utf-8")
        manager = _make_manager_via_subclass(tmp_path)
        cid, _ = manager.start_cursor(
            name_path='FOO/["members"]',
            relative_path="s.py",
        )

        project = manager._project  # type: ignore[attr-defined]
        tool = _bind_tool(CursorConfigureTool, _ToolHarness(manager, project))

        # structural cursors have no LSP edges; edge_types is silently ignored,
        # unknown names therefore don't raise (unlike the LSP-cursor branch).
        view = tool.apply(
            cursor_id=cid,
            edge_types=["this-edge-does-not-exist"],
            include_body=False,
        )
        # the structural projection anchors on the canonical name path and
        # surfaces the kind captured at start time -- no LSP edge metadata
        assert '@ FOO/["members"]' in view
        assert ":container@" in view
