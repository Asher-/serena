"""Shared pytest fixtures for the ``test/serena`` package."""

import re
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from serena.agent import SerenaAgent
from serena.config.serena_config import ProjectConfig, RegisteredProject, SerenaConfig, SerenaPaths
from serena.project import Project
from solidlsp.ls_config import Language
from test.conftest import get_repo_path, language_tests_enabled


@pytest.fixture(scope="session", autouse=True)
def _isolate_explicit_project_root_store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Keep the durable activation store out of the developer's real ``~/.serena``.

    :class:`SerenaAgent` binds :class:`ExplicitProjectRootStore` to
    ``SerenaPaths().explicit_project_roots_file`` at construction, and any
    session-bound ``_activate_project(record_explicit=True)`` then writes there.
    That write is the whole point in production and a defect in a test: before
    the store existed the same call only touched an in-memory dict, so tests
    that exercise activation were harmless, and they silently became writers to
    the real user store the moment it landed. One did — a run on 2026-08-06 left
    ``{"cc-session-record": "/tmp/serena-recorded-root"}`` in a live ``~/.serena``.

    Redirecting the singleton's path once per session covers every agent any
    test builds, including the module-scoped fixtures below, which is why this
    is session-scoped and autouse rather than something each test opts into.

    Deliberately no assertion that the real file went untouched: a serena daemon
    is normally running on the same machine and writes that file legitimately,
    so such a check would be flaky for reasons unrelated to the test suite.
    """
    store = tmp_path_factory.mktemp("serena-explicit-roots") / "session_project_roots.json"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(SerenaPaths(), "explicit_project_roots_file", str(store))
        yield


_WORK_DIR_ROOT = Path(__file__).resolve().parents[2] / "build" / "test"
"""the root of the tests' own directories, under the repository's ``build/``, which ``.gitignore`` excludes"""


@pytest.fixture
def work_dir(request: pytest.FixtureRequest) -> Iterator[Path]:
    """
    :return: a directory of the requesting test's own under the repository's ignored ``build/test/``, unique to this run
        so that concurrent runs never collide, and removed when the test ends. It stands in for ``tmp_path``, whose
        directory lies under the system temp directory that convention://global/organization/script/project/temp forbids.
    """
    # the test's own directory, named after the test so that a leftover one can be traced to it
    _WORK_DIR_ROOT.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix=re.sub(r"[^\w.-]", "_", request.node.name) + "-", dir=_WORK_DIR_ROOT))
    yield directory

    # the directory, with everything the test left in it
    shutil.rmtree(directory)


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
