"""
Tests that ``activate_project`` binds exactly the directory it is given (task://issue_bin_closure_activation_directory.1).

Each test pins one clause of plan://serena/issue_bin_closure_activation_directory:

* symptom://serena/activate-project-stuck-on-parent-repo-for-git-worktree -- an absolute worktree path
  must switch the session away from the repository the worktree belongs to;
* bug://brain/serena/cursor/read/tree/staleness -- every cursor tool must read the newly activated tree;
* bug://brain/serena/project/root/collision -- a name registered for two directories must be refused;
* bug://brain/serena/project/activation/directory/worktree -- activating a directory that holds no
  ``.serena/project.yml`` must write nothing inside it.
"""

import logging
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from serena.agent import SerenaAgent
from serena.config.serena_config import SerenaConfig, SerenaPaths
from serena.tools.config_tools import ActivateProjectTool
from serena.tools.cursor_tools import CursorGrepTool, CursorOverviewTool, CursorStartTool
from test.serena.test_streamable_http_cc_session_key import _make_mcp_ctx

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


def test_activating_a_directory_with_a_data_folder_but_no_project_yml_writes_nothing_inside_it(tmp_path: Path, agent: SerenaAgent) -> None:
    # a repository that tracks .serena/memories and holds no .serena/project.yml
    tracked = tmp_path / "tracked"
    (tracked / ".serena" / "memories").mkdir(parents=True)
    (tracked / ".serena" / "memories" / "note.md").write_text("note\n")
    (tracked / "module.py").write_text("def tracked():\n    return 1\n")
    files_before = _files_under(tracked)

    agent.get_tool(ActivateProjectTool).apply(project=str(tracked))
    overview = agent.get_tool(CursorOverviewTool).apply_ex(relative_path="module.py", because=BECAUSE)
    assert "tracked" in overview, overview

    assert _files_under(tracked) == files_before


def test_loading_a_registered_directory_whose_data_folder_is_gone_writes_nothing_inside_it(tmp_path: Path, agent: SerenaAgent) -> None:
    # a directory registered by an earlier activation, whose data folder has since been removed
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "module.py").write_text("def plain():\n    return 1\n")
    files_before = _files_under(plain)
    agent.get_tool(ActivateProjectTool).apply(project=str(plain))
    home_folder = Path(agent.serena_config.get_home_project_serena_folder(plain))
    assert home_folder.is_dir()
    shutil.rmtree(home_folder)

    # the next instance of the project is loaded from its registration
    registered = next(project for project in agent.serena_config.projects if Path(project.project_root).resolve() == plain.resolve())
    registered._project_instance = None
    registered.get_project_instance(agent.serena_config)

    assert home_folder.is_dir()
    assert _files_under(plain) == files_before


def test_a_configured_location_inside_the_directory_writes_nothing_inside_it(tmp_path: Path, agent: SerenaAgent) -> None:
    # a configured data folder location that lies inside the project directory
    agent.serena_config.project_serena_folder_location = "$projectDir/.data"
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "module.py").write_text("def plain():\n    return 1\n")
    files_before = _files_under(plain)

    agent.get_tool(ActivateProjectTool).apply(project=str(plain))

    assert _files_under(plain) == files_before


def test_a_session_activates_a_worktree_registered_under_its_own_name(tmp_path: Path, agent: SerenaAgent) -> None:
    # a repository and its linked worktree, each registered under a name of its own, as the reported configuration was
    repository = tmp_path / "repository"
    worktree = tmp_path / "vfe-branch"
    repository.mkdir()
    _write_project(repository, "repository", "def in_repository():\n    return 1\n")
    _git(repository, "init", "-q")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-q", "-m", "first")
    _git(repository, "worktree", "add", "-q", "-b", "vfe-branch", str(worktree))
    _write_project(worktree, "vfe-branch", "def in_worktree():\n    return 2\n")

    # both activations are published calls of one forwarded session
    activate = agent.get_tool(ActivateProjectTool)
    context = _make_mcp_ctx(forwarded_header="cc-session-activation-directory")
    activate.apply_ex(mcp_ctx=context, log_call=False, project=str(repository))
    activate.apply_ex(mcp_ctx=context, log_call=False, project=str(worktree))

    overview = agent.get_tool(CursorOverviewTool).apply_ex(mcp_ctx=context, log_call=False, relative_path="module.py", because=BECAUSE)
    assert "in_worktree" in overview, overview


def test_a_session_reads_the_tree_it_activated_last(tmp_path: Path, agent: SerenaAgent) -> None:
    # two projects of one relative path, activated in turn by published calls of one forwarded session
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _write_project(first, "first", "def in_first():\n    return 1\n")
    _write_project(second, "second", "def in_second():\n    return 2\n")
    activate = agent.get_tool(ActivateProjectTool)
    context = _make_mcp_ctx(forwarded_header="cc-session-activation-directory")
    activate.apply_ex(mcp_ctx=context, log_call=False, project=str(first))
    overview = agent.get_tool(CursorOverviewTool).apply_ex(mcp_ctx=context, log_call=False, relative_path="module.py", because=BECAUSE)
    assert "in_first" in overview, overview

    activate.apply_ex(mcp_ctx=context, log_call=False, project=str(second))

    overview = agent.get_tool(CursorOverviewTool).apply_ex(mcp_ctx=context, log_call=False, relative_path="module.py", because=BECAUSE)
    assert "in_second" in overview and "in_first" not in overview, overview


def test_a_session_activating_a_name_of_two_roots_is_refused_with_both(tmp_path: Path, agent: SerenaAgent) -> None:
    # two directories registered under one name, activated by that name in a published call
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _write_project(first, "shared_name", "def in_first():\n    return 1\n")
    _write_project(second, "shared_name", "def in_second():\n    return 2\n")
    activate = agent.get_tool(ActivateProjectTool)
    context = _make_mcp_ctx(forwarded_header="cc-session-activation-directory")
    activate.apply_ex(mcp_ctx=context, log_call=False, project=str(first))
    activate.apply_ex(mcp_ctx=context, log_call=False, project=str(second))

    result = activate.apply_ex(mcp_ctx=context, log_call=False, project="shared_name")

    assert str(first.resolve()) in result and str(second.resolve()) in result, result
