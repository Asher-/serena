"""tests for the activation of a project that is configured for no language server."""

from __future__ import annotations

import logging

import pytest

from serena.agent import SerenaAgent
from serena.config.serena_config import ProjectConfig, RegisteredProject, SerenaConfig
from serena.language_backend import BuiltinLanguageBackend, LanguageBackend
from serena.project import Project, UnconfiguredLanguageServersError
from solidlsp.ls_config import LanguageServerId
from test.conftest import get_repo_path

_PROJECT_NAME = "unconfigured"


def _project(repo_language: LanguageServerId, language_servers: list[LanguageServerId], backend: LanguageBackend) -> Project:
    config = SerenaConfig(gui_log_window=False, web_dashboard=False, log_level=logging.ERROR, language_backend=backend)
    project = Project(
        project_root=str(get_repo_path(repo_language)),
        project_config=ProjectConfig(project_name=_PROJECT_NAME, language_servers=language_servers),
        serena_config=config,
    )
    config.projects = [RegisteredProject.from_project_instance(project)]
    return project


class TestActivationOfProjectWithoutLanguageServers:
    _LSP = BuiltinLanguageBackend.LSP.get_instance()

    def test_verification_over_source_files_raises_naming_the_project_and_its_empty_language_servers(self) -> None:
        project = _project(LanguageServerId.PYTHON, [], self._LSP)

        with pytest.raises(UnconfiguredLanguageServersError) as exc_info:
            project.verify_language_servers_configured()

        message = str(exc_info.value)
        assert f"'{_PROJECT_NAME}'" in message
        assert "language_servers: []" in message
        assert "language_servers: [python]" in message
        assert "project.yml" in message

    def test_verification_over_documentation_only_returns(self) -> None:
        project = _project(LanguageServerId.MARKDOWN, [], self._LSP)

        project.verify_language_servers_configured()

    def test_verification_of_a_project_configured_for_a_language_server_returns(self) -> None:
        project = _project(LanguageServerId.PYTHON, [LanguageServerId.PYTHON], self._LSP)

        project.verify_language_servers_configured()

    def test_activation_over_source_files_fails_naming_the_project(self) -> None:
        project = _project(LanguageServerId.PYTHON, [], self._LSP)
        agent = SerenaAgent(project=None, serena_config=project.serena_config)

        try:
            with pytest.raises(UnconfiguredLanguageServersError) as exc_info:
                agent.activate_project_from_path_or_name(_PROJECT_NAME)
            active_project = agent.get_active_project()
        finally:
            agent.on_shutdown(timeout=5)

        message = str(exc_info.value)
        assert f"'{_PROJECT_NAME}'" in message
        assert "language_servers: []" in message
        assert "language_servers: [python]" in message
        assert "project.yml" in message
        assert active_project is None

    def test_activation_over_documentation_only_succeeds(self) -> None:
        project = _project(LanguageServerId.MARKDOWN, [], self._LSP)
        agent = SerenaAgent(project=None, serena_config=project.serena_config)

        try:
            activated = agent.activate_project_from_path_or_name(_PROJECT_NAME)
        finally:
            agent.on_shutdown(timeout=5)

        assert activated

    def test_reinstating_for_an_instance_that_replaces_none_keeps_the_registered_instance(self) -> None:
        project = _project(LanguageServerId.PYTHON, [], self._LSP)
        registered_project = project.serena_config.projects[0]

        registered_project.reinstate_replaced_instance(project)

        assert registered_project.get_project_instance(project.serena_config) is project

    def test_activation_over_source_files_under_a_non_lsp_backend_succeeds(self) -> None:
        project = _project(LanguageServerId.PYTHON, [], BuiltinLanguageBackend.JETBRAINS.get_instance())
        agent = SerenaAgent(project=None, serena_config=project.serena_config)

        try:
            activated = agent.activate_project_from_path_or_name(_PROJECT_NAME)
        finally:
            agent.on_shutdown(timeout=5)

        assert activated
