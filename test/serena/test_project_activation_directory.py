"""
Tests that ``activate_project`` binds exactly the directory it is given (task://issue_bin_closure_activation_directory.1).

Each test reproduces one issue of plan://serena/issue_bin_closure_activation_directory:

* symptom://serena/activate-project-stuck-on-parent-repo-for-git-worktree -- an absolute worktree path
  must switch the session away from the repository the worktree belongs to;
* bug://brain/serena/cursor/read/tree/staleness -- every cursor tool must read the newly activated tree;
* bug://brain/serena/project/root/collision -- a name registered for two directories must be refused;
* bug://brain/serena/project/activation/directory/worktree -- activating a directory that holds no
  ``.serena/project.yml`` must write nothing inside it.
"""

import logging
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from serena.agent import SerenaAgent
from serena.config.serena_config import SerenaConfig, SerenaPaths
from serena.tools.config_tools import ActivateProjectTool
from serena.tools.cursor_tools import CursorGrepTool, CursorOverviewTool, CursorStartTool

BECAUSE = "to check which project tree this cursor tool reads after an activation"


def _git(cwd: Path, *args: str) -> None:
    """
    Runs a git command in ``cwd`` with a fixed test identity.

    :param cwd: the directory to run the command in
    :param args: the git arguments
    """
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=test@example.com", "-c", "user.name=test", *args],
        check=True,
        capture_output=True,
    )


def _write_project(root: Path, project_name: str, module_source: str) -> None:
    """
    Creates a Python project directory carrying its own ``.serena/project.yml``.

    :param root: the project directory to create
    :param project_name: the name the project's configuration declares
    :param module_source: the source of the project's ``module.py``
    """
    (root / ".serena").mkdir(parents=True)
    (root / ".serena" / "project.yml").write_text(f"project_name: {project_name}\nlanguages:\n- python\n")
    (root / "module.py").write_text(module_source)


def _files_under(root: Path) -> set[Path]:
    """
    :param root: the directory to list
    :return: every file and directory beneath ``root``, as paths relative to it
    """
    return {path.relative_to(root) for path in root.rglob("*")}


@pytest.fixture
def serena_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Redirects the serena home directory into the test's temporary directory.
    """
    home = tmp_path / "serena_home"
    home.mkdir()
    monkeypatch.setattr(SerenaPaths(), "serena_user_home_dir", str(home))
    return home


@pytest.fixture
def agent(serena_home: Path) -> Iterator[SerenaAgent]:
    """
    An agent with no registered project and no active project.
    """
    config = SerenaConfig(gui_log_window=False, web_dashboard=False, log_level=logging.ERROR)
    config.projects = []
    serena_agent = SerenaAgent(project=None, serena_config=config)
    try:
        yield serena_agent
    finally:
        serena_agent.on_shutdown(timeout=5)


def test_absolute_worktree_path_switches_away_from_its_repository(tmp_path: Path, agent: SerenaAgent) -> None:
    # a repository whose tracked project.yml the worktree shares, so both register under one name
    repo = tmp_path / "repo"
    _write_project(repo, "shared_name", "def shared():\n    return 1\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    worktree = tmp_path / "worktree"
    _git(repo, "worktree", "add", "-q", "-b", "feature", str(worktree))
    (worktree / "only_in_worktree.py").write_text("def only_in_worktree():\n    return 2\n")

    # activate the repository, then the worktree by its absolute path
    activate = agent.get_tool(ActivateProjectTool)
    activate.apply(project=str(repo))
    activate.apply(project=str(worktree))

    active = agent.get_active_project()
    assert active is not None
    assert active.project_root == str(worktree.resolve())
    started = agent.get_tool(CursorStartTool).apply(name_path="only_in_worktree", relative_path="only_in_worktree.py", because=BECAUSE)
    assert started.startswith("Started cursor"), started
    assert "only_in_worktree" in started


def test_cursor_tools_read_the_newly_activated_tree(tmp_path: Path, agent: SerenaAgent) -> None:
    # two projects whose module.py defines different functions
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    _write_project(project_a, "project_a", "def only_in_a():\n    return 1\n")
    _write_project(project_b, "project_b", "def only_in_b():\n    return 2\n")

    # open a cursor in A, then switch to B
    activate = agent.get_tool(ActivateProjectTool)
    activate.apply(project=str(project_a))
    started_in_a = agent.get_tool(CursorStartTool).apply(name_path="only_in_a", relative_path="module.py", because=BECAUSE)
    assert started_in_a.startswith("Started cursor"), started_in_a
    activate.apply(project=str(project_b))

    overview = agent.get_tool(CursorOverviewTool).apply(relative_path="module.py", because=BECAUSE)
    assert "only_in_b" in overview and "only_in_a" not in overview, overview
    started_in_b = agent.get_tool(CursorStartTool).apply(name_path="only_in_b", relative_path="module.py", because=BECAUSE)
    assert started_in_b.startswith("Started cursor"), started_in_b
    grep = agent.get_tool(CursorGrepTool).apply(substring_pattern=r"def only_in_", relative_path="module.py", because=BECAUSE)
    assert "only_in_b" in grep and "only_in_a" not in grep, grep


def test_activation_by_a_name_registered_for_two_directories_refuses_and_lists_both(tmp_path: Path, agent: SerenaAgent) -> None:
    # two directories registered under one name
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_project(first, "twin", "def first():\n    return 1\n")
    _write_project(second, "twin", "def second():\n    return 2\n")
    activate = agent.get_tool(ActivateProjectTool)
    activate.apply(project=str(first))
    activate.apply(project=str(second))
    active_before = agent.get_active_project()

    result = activate.apply_ex(project="twin")

    assert str(first.resolve()) in result and str(second.resolve()) in result, result
    assert agent.get_active_project() is active_before


def test_activating_a_directory_without_project_yml_writes_nothing_inside_it(tmp_path: Path, agent: SerenaAgent) -> None:
    # a plain directory holding source but no .serena/project.yml
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "module.py").write_text("def plain():\n    return 1\n")
    files_before = _files_under(plain)

    # activate it and read through a cursor tool, which saves the language server caches
    agent.get_tool(ActivateProjectTool).apply(project=str(plain))
    active = agent.get_active_project()
    assert active is not None and active.project_root == str(plain.resolve())
    overview = agent.get_tool(CursorOverviewTool).apply_ex(relative_path="module.py", because=BECAUSE)
    assert "plain" in overview, overview

    assert _files_under(plain) == files_before
