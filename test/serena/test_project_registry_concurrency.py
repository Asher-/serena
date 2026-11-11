import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from serena.config.serena_config import (
    DEFAULT_PROJECT_SERENA_FOLDER_LOCATION,
    ProjectConfigAutoGenerationMode,
    RegisteredProject,
    SerenaConfig,
    SerenaPaths,
)
from serena.project import Project

_THREAD_COUNT = 8


@pytest.fixture
def project_root(work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    serena_home = work_dir / "serena_home"
    serena_home.mkdir()
    monkeypatch.setattr(SerenaPaths(), "serena_user_home_dir", str(serena_home))
    root = work_dir / "myproject"
    root.mkdir()
    return root


def _config() -> SerenaConfig:
    return SerenaConfig(gui_log_window=False, web_dashboard=False, project_serena_folder_location=DEFAULT_PROJECT_SERENA_FOLDER_LOCATION)


def _call_at_once(call: Callable[[], Project]) -> tuple[list[Project], list[BaseException]]:
    results: list[Project] = []
    errors: list[BaseException] = []
    all_ready = threading.Barrier(_THREAD_COUNT, timeout=30)

    def run() -> None:
        all_ready.wait()
        try:
            results.append(call())
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(_THREAD_COUNT)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)

    # the background scan of every project built, which would otherwise outlive the test's directory
    for project in results:
        project._ignore_spec_available.wait()
    return results, errors


def test_concurrent_loads_of_one_registered_project_return_one_instance(project_root: Path) -> None:
    config = _config()
    registered_project = RegisteredProject.from_project_root(
        project_root, serena_config=config, autogen=ProjectConfigAutoGenerationMode.SYNCHRONOUS
    )

    instances, errors = _call_at_once(lambda: registered_project.get_project_instance(config))

    assert errors == []
    assert len(instances) == _THREAD_COUNT
    assert all(instance is instances[0] for instance in instances)


def test_concurrent_registrations_of_one_directory_register_it_once(project_root: Path) -> None:
    config = _config()

    added, errors = _call_at_once(lambda: config.add_project_from_path(project_root))

    assert len(added) == 1
    assert len(errors) == _THREAD_COUNT - 1
    assert all(isinstance(error, FileExistsError) for error in errors)
    assert [str(project.project_root) for project in config.projects] == [str(project_root.resolve())]


def test_concurrent_additions_and_removals_of_distinct_projects_leave_the_registry_empty(project_root: Path) -> None:
    config = SerenaConfig.from_config_file()
    registered_projects = []
    for i in range(_THREAD_COUNT):
        root = project_root.parent / f"project_{i}"
        root.mkdir()
        registered_projects.append(
            RegisteredProject.from_project_root(root, serena_config=config, autogen=ProjectConfigAutoGenerationMode.SYNCHRONOUS)
        )
    errors: list[BaseException] = []
    all_ready = threading.Barrier(_THREAD_COUNT, timeout=30)

    def add_and_remove(registered_project: RegisteredProject) -> None:
        all_ready.wait()
        try:
            for _ in range(20):
                config.add_registered_project(registered_project)
                config.remove_registered_project(registered_project)
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=add_and_remove, args=(registered_project,)) for registered_project in registered_projects]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert not any(thread.is_alive() for thread in threads)

    assert errors == []
    assert config.projects == []
    assert SerenaConfig.from_config_file().projects == []
