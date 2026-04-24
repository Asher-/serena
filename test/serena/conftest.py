"""Shared pytest fixtures for the ``test/serena`` package."""

from collections.abc import Iterator

import pytest

from serena.agent import SerenaAgent
from serena.config.serena_config import ProjectConfig, RegisteredProject, SerenaConfig
from serena.project import Project
from solidlsp.ls_config import Language
from test.conftest import get_repo_path, language_tests_enabled


@pytest.fixture(scope="module")
def python_serena_agent() -> Iterator[SerenaAgent]:
    """SerenaAgent configured for the Python test repo."""
    if not language_tests_enabled(Language.PYTHON):
        pytest.skip("Python tests not enabled")

    config = SerenaConfig(gui_log_window=False, web_dashboard=False)
    repo_path = get_repo_path(Language.PYTHON)
    project = Project(
        project_root=str(repo_path),
        project_config=ProjectConfig(
            project_name="test_repo_python",
            languages=[Language.PYTHON],
            ignored_paths=[],
            excluded_tools=[],
            read_only=False,
            ignore_all_files_in_gitignore=True,
            initial_prompt="",
            encoding="utf-8",
        ),
        serena_config=config,
    )
    config.projects = [RegisteredProject.from_project_instance(project)]
    agent = SerenaAgent(project="test_repo_python", serena_config=config)
    agent.execute_task(lambda: None)
    yield agent
    agent.on_shutdown(timeout=5)
