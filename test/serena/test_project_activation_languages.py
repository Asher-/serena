"""tests for the languages a newly configured project is given, and for its activation, neither of which lists a directory of the project tree."""

from __future__ import annotations

import functools
import logging
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from serena.agent import SerenaAgent
from serena.config.serena_config import ProjectConfig, RegisteredProject, SerenaConfig
from serena.project import Project
from solidlsp.ls_config import LanguageServerId


def _serena_config() -> SerenaConfig:
    config = SerenaConfig(gui_log_window=False, web_dashboard=False, log_level=logging.ERROR)
    config.projects = []
    return config


_GIT = os.path.join(subprocess.run(["git", "--exec-path"], check=True, capture_output=True, text=True).stdout.strip(), "git")
"""
the git binary itself, resolved once at import: where ``git`` on PATH is a launcher (as macOS's /usr/bin/git is), each
call through it costs a tool lookup that would otherwise put a test over its time bound
"""


@functools.cache
def _git_layout() -> Path:
    """
    :return: a directory holding ``main``, made by ``git init``, and ``worktree``, a linked worktree of it made by
        ``git worktree add --relative-paths``, so that the pair stays valid when copied elsewhere; git runs once per
        module, not once per test
    """
    root = Path(tempfile.mkdtemp(prefix="activation-git-layout-"))
    main = root / "main"
    subprocess.run([_GIT, "init", "--quiet", str(main)], check=True, capture_output=True)
    subprocess.run(
        [_GIT, "-C", str(main), "worktree", "add", "--quiet", "--relative-paths", "--orphan", "-b", "worktree", str(root / "worktree")],
        check=True,
        capture_output=True,
    )
    return root


@pytest.fixture(scope="module", autouse=True)
def _git_layout_made_in_module_setup() -> None:
    """
    Makes the git layout (:func:`_git_layout`) while the module is set up, so that no test's own time includes running git
    """
    _git_layout()


def _main_checkout_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """
    :return: a main checkout and a linked worktree of it, copied from the layout git made (:func:`_git_layout`): the
        worktree's ``.git`` file names its git directory under the main checkout's ``.git/worktrees``, whose
        ``commondir`` names the main ``.git``
    """
    shutil.copytree(_git_layout(), tmp_path, symlinks=True, dirs_exist_ok=True)
    main = tmp_path / "main"
    (main / "a.py").write_text("x = 1\n")
    return main, tmp_path / "worktree"


def _register(config: SerenaConfig, root: Path, language_servers: list[LanguageServerId]) -> None:
    project = Project(
        project_root=str(root),
        project_config=ProjectConfig(project_name=root.name, language_servers=language_servers),
        serena_config=config,
    )
    config.projects.append(RegisteredProject.from_project_instance(project))


@pytest.fixture
def no_tree_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """
    Records every directory listed through os.scandir, os.listdir or os.walk, from any thread, for the whole test;
    the test fails if any directory of its trees (under tmp_path) was listed.
    """
    listed: list[str] = []

    def recording(real: Callable[..., Any]) -> Callable[..., Any]:
        def record(path: Any = ".", *args: Any, **kwargs: Any) -> Any:
            if not isinstance(path, int):
                listed.append(os.path.realpath(os.fspath(path)))
            return real(path, *args, **kwargs)

        return record

    for name in ("scandir", "listdir", "walk"):
        monkeypatch.setattr(os, name, recording(getattr(os, name)))
    yield
    root = os.path.realpath(tmp_path)
    scanned = [path for path in listed if path == root or path.startswith(root + os.sep)]
    assert not scanned, f"directories of the project trees were listed: {scanned}"


class TestLanguagesOfANewProject:
    def test_a_worktree_takes_the_languages_of_its_registered_main_checkout(self, tmp_path: Path, no_tree_scan: None) -> None:
        config = _serena_config()
        main, worktree = _main_checkout_with_worktree(tmp_path)
        _register(config, main, [LanguageServerId.PYTHON, LanguageServerId.GO])

        project_config = ProjectConfig.autogenerate(worktree, config, save_to_disk=False)

        assert project_config.language_servers == [LanguageServerId.PYTHON, LanguageServerId.GO]

    def test_a_worktree_whose_main_checkout_is_not_registered_has_no_languages(self, tmp_path: Path, no_tree_scan: None) -> None:
        config = _serena_config()
        _, worktree = _main_checkout_with_worktree(tmp_path)

        project_config = ProjectConfig.autogenerate(worktree, config, save_to_disk=False)

        assert project_config.language_servers == []

    def test_a_main_checkout_has_no_languages_without_a_scan(self, tmp_path: Path, no_tree_scan: None) -> None:
        config = _serena_config()
        main, _ = _main_checkout_with_worktree(tmp_path)

        project_config = ProjectConfig.autogenerate(main, config, save_to_disk=False)

        assert project_config.language_servers == []

    def test_a_git_file_naming_no_git_directory_gives_no_languages(self, tmp_path: Path, no_tree_scan: None) -> None:
        config = _serena_config()
        main, worktree = _main_checkout_with_worktree(tmp_path)
        _register(config, main, [LanguageServerId.PYTHON])
        (worktree / ".git").write_text("not a git pointer\n")

        project_config = ProjectConfig.autogenerate(worktree, config, save_to_disk=False)

        assert project_config.language_servers == []

    def test_a_git_directory_without_commondir_gives_no_languages(self, tmp_path: Path, no_tree_scan: None) -> None:
        config = _serena_config()
        main, worktree = _main_checkout_with_worktree(tmp_path)
        _register(config, main, [LanguageServerId.PYTHON])
        (main / ".git" / "worktrees" / "worktree" / "commondir").unlink()

        project_config = ProjectConfig.autogenerate(worktree, config, save_to_disk=False)

        assert project_config.language_servers == []

    def test_a_worktree_of_a_bare_repository_gives_no_languages(self, tmp_path: Path, no_tree_scan: None) -> None:
        config = _serena_config()
        main, worktree = _main_checkout_with_worktree(tmp_path)
        _register(config, main, [LanguageServerId.PYTHON])
        bare_worktree_git_dir = tmp_path / "repo.git" / "worktrees" / "worktree"
        bare_worktree_git_dir.mkdir(parents=True)
        (bare_worktree_git_dir / "commondir").write_text("../..\n")
        (worktree / ".git").write_text(f"gitdir: {bare_worktree_git_dir}\n")

        project_config = ProjectConfig.autogenerate(worktree, config, save_to_disk=False)

        assert project_config.language_servers == []

    def test_an_undecodable_git_file_gives_no_languages(self, tmp_path: Path, no_tree_scan: None) -> None:
        config = _serena_config()
        main, worktree = _main_checkout_with_worktree(tmp_path)
        _register(config, main, [LanguageServerId.PYTHON])
        (worktree / ".git").write_bytes(b"gitdir: \xff\xfe\n")

        project_config = ProjectConfig.autogenerate(worktree, config, save_to_disk=False)

        assert project_config.language_servers == []

    def test_an_undecodable_commondir_gives_no_languages(self, tmp_path: Path, no_tree_scan: None) -> None:
        config = _serena_config()
        main, worktree = _main_checkout_with_worktree(tmp_path)
        _register(config, main, [LanguageServerId.PYTHON])
        (main / ".git" / "worktrees" / "worktree" / "commondir").write_bytes(b"\xff\xfe\n")

        project_config = ProjectConfig.autogenerate(worktree, config, save_to_disk=False)

        assert project_config.language_servers == []


class TestActivationOfANewWorktree:
    def test_activation_returns_within_a_second_with_the_main_checkouts_languages(self, tmp_path: Path, no_tree_scan: None) -> None:
        config = _serena_config()
        main, worktree = _main_checkout_with_worktree(tmp_path)
        _register(config, main, [LanguageServerId.PYTHON])
        agent = SerenaAgent(project=None, serena_config=config)

        try:
            started = time.monotonic()
            activated = agent.activate_project_from_path_or_name(str(worktree))
            elapsed = time.monotonic() - started
            active = agent.get_active_project()
        finally:
            agent.on_shutdown(timeout=5)

        assert activated
        assert elapsed <= 1.0
        assert active is not None
        assert active.project_config.language_servers == [LanguageServerId.PYTHON]
