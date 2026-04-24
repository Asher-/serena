"""
FindSymbolTool parity tests for the statement-widening body formatter.

Motivation: :class:`serena.tools.symbol_tools.FindSymbolTool` with
``include_body=True`` must emit the statement-widened body text that
``cursor_configure`` already uses. Without widening, Python ``Variable``
symbols return identifier-only bodies (just ``FOO``) rather than the full
multi-line assignment. These tests confirm the widening parity added to
``FindSymbolTool.apply`` and guard against regressions where the tool-layer
path diverges again from the cursor-view path.
"""

import json
import os
from pathlib import Path

from serena.agent import SerenaAgent
from serena.tools.symbol_tools import FindSymbolTool


def _find_symbol_body(result_json: str, name_path: str) -> str:
    """Extract the ``body`` string for the unique symbol with ``name_path`` from a FindSymbolTool result."""
    payload = json.loads(result_json)
    # FindSymbolTool groups by relative_path; iterate all groups and flatten symbol dicts
    collected: list[dict] = []
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list):
                collected.extend(value)
    elif isinstance(payload, list):
        collected.extend(payload)

    matches = [s for s in collected if s.get("name_path") == name_path]
    assert len(matches) == 1, f"Expected exactly one match for {name_path!r}; got {len(matches)} in {payload}"
    body = matches[0].get("body")
    assert isinstance(body, str), f"body missing or not a string for {name_path!r}: {matches[0]}"
    return body


class TestFindSymbolIncludeBodyWidening:
    """Regression: FindSymbolTool include_body=True must return statement-widened bodies."""

    def test_include_body_widens_python_multiline_variable_literal(self, python_serena_agent: SerenaAgent) -> None:
        """FindSymbolTool(include_body=True) for a Python variable-kind symbol must return the full
        multi-line assignment, not the identifier-only LSP range.

        Parity with :meth:`CursorConfigureTool.apply` — the cursor_configure path already widens;
        FindSymbolTool now shares the same widening helper and must produce identical body text.
        """
        find_symbol_tool = python_serena_agent.get_tool(FindSymbolTool)

        # sandbox fixture: multi-line list literal bound to FOO
        project_root = Path(python_serena_agent.get_active_project_or_raise().project_root)
        rel_path = os.path.join("test_repo", "_find_symbol_widening_sandbox.py")
        abs_path = project_root / rel_path
        abs_path.write_text("FOO = [\n    1,\n    2,\n    3,\n]\n")

        try:
            python_serena_agent.reset_language_server_manager()
            result_json = find_symbol_tool.apply(
                name_path_pattern="FOO",
                relative_path=rel_path,
                include_body=True,
            )

            # the returned body must span the full assignment statement
            body = _find_symbol_body(result_json, name_path="FOO")
            assert "FOO = [" in body, f"Assignment start missing from body: {body!r}"
            assert "1," in body, f"First literal element missing from body: {body!r}"
            assert "2," in body, f"Second literal element missing from body: {body!r}"
            assert "3," in body, f"Third literal element missing from body: {body!r}"
            assert "]" in body, f"Closing bracket missing from body (widening regression): {body!r}"
        finally:
            if abs_path.exists():
                abs_path.unlink()
            try:
                python_serena_agent.reset_language_server_manager()
            except Exception:
                pass

    def test_include_body_is_unchanged_for_non_variable_kinds(self, python_serena_agent: SerenaAgent) -> None:
        """Widening must be a no-op for symbols whose LSP extent is already statement-scoped.

        Functions/methods/classes already have full-statement extents; the widening helper
        returns ``None`` for these, and :meth:`LanguageServerSymbol.body` (the LSP range)
        continues to be used. This confirms widening does not truncate or alter those bodies.
        """
        find_symbol_tool = python_serena_agent.get_tool(FindSymbolTool)

        project_root = Path(python_serena_agent.get_active_project_or_raise().project_root)
        rel_path = os.path.join("test_repo", "_find_symbol_widening_function_sandbox.py")
        abs_path = project_root / rel_path
        abs_path.write_text('def greet(name: str) -> str:\n    return f"hello, {name}"\n')

        try:
            python_serena_agent.reset_language_server_manager()
            result_json = find_symbol_tool.apply(
                name_path_pattern="greet",
                relative_path=rel_path,
                include_body=True,
            )

            body = _find_symbol_body(result_json, name_path="greet")
            # function body must include both the signature and the return statement unchanged
            assert "def greet(name: str) -> str:" in body, f"Signature missing from function body: {body!r}"
            assert 'return f"hello, {name}"' in body, f"Return statement missing from function body: {body!r}"
        finally:
            if abs_path.exists():
                abs_path.unlink()
            try:
                python_serena_agent.reset_language_server_manager()
            except Exception:
                pass

    def test_include_body_widens_dataclass_field(self, python_serena_agent: SerenaAgent) -> None:
        """Dataclass ``field`` assignments are reported as Variable by the Python LSP with
        identifier-only extents; FindSymbolTool(include_body=True) must widen to the
        full ``name: type = default`` assignment.
        """
        find_symbol_tool = python_serena_agent.get_tool(FindSymbolTool)

        project_root = Path(python_serena_agent.get_active_project_or_raise().project_root)
        rel_path = os.path.join("test_repo", "_find_symbol_widening_dataclass_sandbox.py")
        abs_path = project_root / rel_path
        abs_path.write_text(
            "from dataclasses import dataclass, field\n"
            "\n"
            "@dataclass\n"
            "class Holder:\n"
            "    values: list[int] = field(default_factory=lambda: [\n"
            "        10,\n"
            "        20,\n"
            "        30,\n"
            "    ])\n"
        )

        try:
            python_serena_agent.reset_language_server_manager()
            result_json = find_symbol_tool.apply(
                name_path_pattern="Holder/values",
                relative_path=rel_path,
                include_body=True,
            )

            body = _find_symbol_body(result_json, name_path="Holder/values")
            # widened body must span the full annotated assignment including the multi-line default
            assert "values: list[int]" in body, f"Annotation missing from widened body: {body!r}"
            assert "field(default_factory" in body, f"field(...) call missing from widened body: {body!r}"
            assert "10," in body, f"First literal element missing from widened body: {body!r}"
            assert "20," in body, f"Second literal element missing from widened body: {body!r}"
            assert "30," in body, f"Third literal element missing from widened body: {body!r}"
        finally:
            if abs_path.exists():
                abs_path.unlink()
            try:
                python_serena_agent.reset_language_server_manager()
            except Exception:
                pass
