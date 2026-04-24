"""unit tests for config-style tools in :mod:`serena.tools.config_tools`.

the :class:`GetLanguageServerStatusTool` tests here are the MCP-tool counterpart of
``test_dashboard.py::test_language_server_status_reports_active_and_unavailable`` — both
exercise the same agent accessors (``get_active_lsp_languages`` / ``get_unavailable_lsp_languages``)
through different front-doors (the dashboard HTTP endpoint vs. the MCP tool), so keeping the
shape assertions parallel guards against the two surfaces drifting apart.
"""

from __future__ import annotations

import json

from serena.tools.config_tools import GetLanguageServerStatusTool
from solidlsp.ls_config import Language


class _DummyAgent:
    """the smallest surface a :class:`Tool` actually needs when invoked via ``apply()`` directly:

    the tool does not go through ``apply_ex`` in these tests, so we can skip the task-executor,
    tool-registry, and project-state machinery and just expose the two accessors the tool reads.
    """

    def __init__(
        self,
        active_lsp_languages: list[Language] | None = None,
        unavailable_lsp_languages: dict[Language, Exception] | None = None,
    ) -> None:
        self._active_lsp_languages = list(active_lsp_languages or [])
        self._unavailable_lsp_languages = dict(unavailable_lsp_languages or {})

    def get_active_lsp_languages(self) -> list[Language]:
        return list(self._active_lsp_languages)

    def get_unavailable_lsp_languages(self) -> dict[Language, Exception]:
        return dict(self._unavailable_lsp_languages)


def _make_tool(
    active_lsp_languages: list[Language] | None = None,
    unavailable_lsp_languages: dict[Language, Exception] | None = None,
) -> GetLanguageServerStatusTool:
    agent = _DummyAgent(
        active_lsp_languages=active_lsp_languages,
        unavailable_lsp_languages=unavailable_lsp_languages,
    )
    # Tool's base constructor only stores ``agent`` on self — SimpleNamespace-style duck typing is
    # enough for apply() because it never touches tool_registry, execute_task, or project state.
    return GetLanguageServerStatusTool(agent=agent)  # type: ignore[arg-type]


def test_get_language_server_status_returns_empty_when_no_manager_state() -> None:
    tool = _make_tool(active_lsp_languages=[], unavailable_lsp_languages={})

    payload = json.loads(tool.apply())

    assert payload == {"active": [], "unavailable": {}}


def test_get_language_server_status_reports_active_and_unavailable() -> None:
    # mirrors test_dashboard.test_language_server_status_reports_active_and_unavailable: Python is
    # up, Scala is down with an exception — the tool must serialize both shape-for-shape the same
    # way the dashboard's /get_language_server_status endpoint does
    boom = RuntimeError("Metals crashed on startup")
    tool = _make_tool(
        active_lsp_languages=[Language.PYTHON],
        unavailable_lsp_languages={Language.SCALA: boom},
    )

    payload = json.loads(tool.apply())

    assert payload == {
        "active": [Language.PYTHON.value],
        "unavailable": {Language.SCALA.value: "Metals crashed on startup"},
    }


def test_get_language_server_status_sorts_active_languages() -> None:
    # agents should be able to rely on a stable ordering of ``active`` so diffs between snapshots
    # are meaningful; this mirrors the dashboard's sort-by-value contract
    tool = _make_tool(
        active_lsp_languages=[Language.TYPESCRIPT, Language.MARKDOWN, Language.PYTHON],
        unavailable_lsp_languages={},
    )

    payload = json.loads(tool.apply())

    assert payload["active"] == sorted([Language.TYPESCRIPT.value, Language.MARKDOWN.value, Language.PYTHON.value])


def test_get_language_server_status_serializes_language_keys_as_values() -> None:
    # unavailable is a dict keyed by Language enum in-process; the serialized form must key by the
    # enum's .value string (not its repr) so the dashboard JS and MCP clients see the same shape
    tool = _make_tool(
        active_lsp_languages=[Language.PYTHON],
        unavailable_lsp_languages={Language.SCALA: RuntimeError("boom"), Language.GO: ValueError("nope")},
    )

    payload = json.loads(tool.apply())

    assert set(payload["unavailable"].keys()) == {Language.SCALA.value, Language.GO.value}
    assert payload["unavailable"][Language.SCALA.value] == "boom"
    assert payload["unavailable"][Language.GO.value] == "nope"
