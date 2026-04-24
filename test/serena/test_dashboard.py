from types import SimpleNamespace

from serena.dashboard import SerenaDashboardAPI
from solidlsp.ls_config import Language


class _DummyMemoryLogHandler:
    def get_log_messages(self, from_idx: int = 0):  # pragma: no cover - simple stub
        return SimpleNamespace(messages=[], max_idx=-1)

    def clear_log_messages(self) -> None:  # pragma: no cover - simple stub
        pass


class _DummyAgent:
    def __init__(
        self,
        project: SimpleNamespace | None,
        active_lsp_languages: list[Language] | None = None,
        unavailable_lsp_languages: dict[Language, Exception] | None = None,
    ) -> None:
        self._project = project
        self._active_lsp_languages = active_lsp_languages or []
        self._unavailable_lsp_languages = unavailable_lsp_languages or {}

    def execute_task(self, func, *, logged: bool | None = None, name: str | None = None):
        del logged, name
        return func()

    def get_active_project(self):
        return self._project

    def get_active_lsp_languages(self) -> list[Language]:
        return list(self._active_lsp_languages)

    def get_unavailable_lsp_languages(self) -> dict[Language, Exception]:
        return dict(self._unavailable_lsp_languages)


def _make_dashboard(
    project_languages: list[Language] | None,
    active_lsp_languages: list[Language] | None = None,
    unavailable_lsp_languages: dict[Language, Exception] | None = None,
) -> SerenaDashboardAPI:
    project = None
    if project_languages is not None:
        project = SimpleNamespace(project_config=SimpleNamespace(languages=project_languages))
    agent = _DummyAgent(
        project,
        active_lsp_languages=active_lsp_languages,
        unavailable_lsp_languages=unavailable_lsp_languages,
    )
    return SerenaDashboardAPI(memory_log_handler=_DummyMemoryLogHandler(), tool_names=[], agent=agent, tool_usage_stats=None)


def test_available_languages_include_experimental_when_no_active_project():
    dashboard = _make_dashboard(project_languages=None)
    response = dashboard._get_available_languages()
    expected = sorted(lang.value for lang in Language.iter_all(include_experimental=True))
    assert response.languages == expected


def test_available_languages_exclude_project_languages():
    dashboard = _make_dashboard(project_languages=[Language.PYTHON, Language.MARKDOWN])
    response = dashboard._get_available_languages()
    available = set(response.languages)
    assert Language.PYTHON.value not in available
    assert Language.MARKDOWN.value not in available
    # ensure experimental languages remain available for selection
    assert Language.ANSIBLE.value in available


def test_language_server_status_empty_when_no_manager_state():
    dashboard = _make_dashboard(
        project_languages=[Language.PYTHON],
        active_lsp_languages=[],
        unavailable_lsp_languages={},
    )
    response = dashboard._get_language_server_status()
    assert response.active == []
    assert response.unavailable == {}


def test_language_server_status_reports_active_and_unavailable():
    boom = RuntimeError("Metals crashed on startup")
    dashboard = _make_dashboard(
        project_languages=[Language.PYTHON, Language.SCALA],
        active_lsp_languages=[Language.PYTHON],
        unavailable_lsp_languages={Language.SCALA: boom},
    )
    response = dashboard._get_language_server_status()
    assert response.active == [Language.PYTHON.value]
    assert response.unavailable == {Language.SCALA.value: "Metals crashed on startup"}


def test_language_server_status_sorts_active_languages():
    dashboard = _make_dashboard(
        project_languages=[Language.PYTHON, Language.TYPESCRIPT, Language.MARKDOWN],
        active_lsp_languages=[Language.TYPESCRIPT, Language.MARKDOWN, Language.PYTHON],
        unavailable_lsp_languages={},
    )
    response = dashboard._get_language_server_status()
    assert response.active == sorted([Language.TYPESCRIPT.value, Language.MARKDOWN.value, Language.PYTHON.value])
