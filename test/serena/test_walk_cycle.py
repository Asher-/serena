"""tests of the walks that follow symbolic links to directories, over trees whose links lead back into the tree or alias
one directory.

a following walk enters each real directory at most once: a link to the walk's root or to an ancestor adds no path, two
links to one directory gather its files once, and a link to a directory the walk does not otherwise reach is followed.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from serena.config.serena_config import ProjectConfig, SerenaConfig
from serena.language_backend import BuiltinLanguageBackend
from serena.memories.memory_manager import MemoryManager
from serena.project import Project
from serena.util.file_system import walk_dir_entries
from solidlsp.ls_config import LanguageServerId


def _link(link: Path, target: Path) -> None:
    """
    Creates a symbolic link to a directory, skipping the test where the platform refuses one.

    :param link: the path of the link
    :param target: the directory the link points to
    """
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"cannot create directory symlinks on this platform/permissions: {e}")


@pytest.fixture
def looped_tree(tmp_path: Path) -> Path:
    """
    :return: the root of a tree holding ``pkg/module.py`` and ``pkg/loop``, a symbolic link to the root itself
    """
    root = tmp_path / "root"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "module.py").write_text("NEEDLE = 1\n", encoding="utf-8")
    _link(root / "pkg" / "loop", root)
    return root


def _walked_files(root: Path) -> list[str]:
    """
    :param root: the directory to walk, following links
    :return: the paths, relative to ``root``, of every file the walk yields, in the order yielded
    """
    return [
        Path(os.path.relpath(entry.path, root)).as_posix()
        for _directory, _subdirectories, files in walk_dir_entries(str(root), followlinks=True)
        for entry in files
    ]


def test_a_following_walk_yields_each_file_once_when_a_link_leads_back_to_the_root(looped_tree: Path) -> None:
    assert _walked_files(looped_tree) == ["pkg/module.py"]


def test_a_following_walk_yields_each_file_once_when_a_link_leads_to_an_ancestor(looped_tree: Path) -> None:
    (looped_tree / "pkg" / "deep").mkdir()
    _link(looped_tree / "pkg" / "deep" / "up", looped_tree / "pkg")

    assert sorted(_walked_files(looped_tree)) == ["pkg/module.py"]


def test_a_following_walk_gathers_the_files_of_a_directory_two_links_alias_once(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "x.py").write_text("X = 1\n", encoding="utf-8")
    _link(root / "first", shared)
    _link(root / "second", shared)

    walked = _walked_files(root)

    assert len(walked) == 1
    assert Path(walked[0]).name == "x.py"


def test_a_following_walk_names_a_directory_by_its_own_path_when_a_link_to_it_comes_first(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "z" / "pkg").mkdir(parents=True)
    (root / "z" / "pkg" / "x.py").write_text("X = 1\n", encoding="utf-8")
    _link(root / "a_link", root / "z" / "pkg")

    assert _walked_files(root) == ["z/pkg/x.py"]


def test_a_following_walk_names_a_directory_by_its_own_path_when_the_directory_comes_first(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "a" / "pkg").mkdir(parents=True)
    (root / "a" / "pkg" / "x.py").write_text("X = 1\n", encoding="utf-8")
    _link(root / "z_link", root / "a" / "pkg")

    assert _walked_files(root) == ["a/pkg/x.py"]


def test_a_following_walk_does_not_enter_again_a_sibling_it_has_entered(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "x.py").write_text("X = 1\n", encoding="utf-8")
    (root / "zz").mkdir()
    _link(root / "zz" / "again", root / "pkg")

    assert _walked_files(root) == ["pkg/x.py"]


def test_a_following_walk_enters_a_directory_it_skips_once_through_a_link_to_it(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "skipped" / "inner").mkdir(parents=True)
    (root / "skipped" / "inner" / "x.py").write_text("X = 1\n", encoding="utf-8")
    _link(root / "first", root / "skipped" / "inner")
    _link(root / "second", root / "skipped" / "inner")

    walked = [
        Path(os.path.relpath(entry.path, root)).as_posix()
        for _directory, _subdirectories, files in walk_dir_entries(
            str(root), followlinks=True, is_ignored_dir=lambda entry: entry.name == "skipped"
        )
        for entry in files
    ]

    assert walked == ["first/x.py"]


def test_a_following_walk_names_a_directory_only_links_reach_by_the_first_link_in_name_order(tmp_path: Path) -> None:
    # links made in reverse name order, so that neither creation nor listing order agrees with name order by design
    root = tmp_path / "root"
    (root / "skipped" / "inner").mkdir(parents=True)
    (root / "skipped" / "inner" / "x.py").write_text("X = 1\n", encoding="utf-8")
    for name in ["link_e", "link_d", "link_c", "link_b", "link_a"]:
        _link(root / name, root / "skipped" / "inner")

    walked = [
        Path(os.path.relpath(entry.path, root)).as_posix()
        for _directory, _subdirectories, files in walk_dir_entries(
            str(root), followlinks=True, is_ignored_dir=lambda entry: entry.name == "skipped"
        )
        for entry in files
    ]

    assert walked == ["link_a/x.py"]


def test_a_following_walk_names_a_nested_directory_under_the_link_met_first_in_name_order(tmp_path: Path) -> None:
    # a link to a skipped directory and a link to its subdirectory, the latter first in name order
    root = tmp_path / "root"
    (root / "skipped" / "sub").mkdir(parents=True)
    (root / "skipped" / "sub" / "x.py").write_text("X = 1\n", encoding="utf-8")
    _link(root / "z_outer", root / "skipped")
    _link(root / "a_sub", root / "skipped" / "sub")

    walked = [
        Path(os.path.relpath(entry.path, root)).as_posix()
        for _directory, _subdirectories, files in walk_dir_entries(
            str(root), followlinks=True, is_ignored_dir=lambda entry: entry.name == "skipped"
        )
        for entry in files
    ]

    assert walked == ["a_sub/x.py"]


def test_a_following_walk_does_not_enter_a_link_its_ignore_rule_holds_for(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "x.py").write_text("X = 1\n", encoding="utf-8")
    _link(root / ".hidden", shared)

    walked = [
        entry.name
        for _directory, _subdirectories, files in walk_dir_entries(
            str(root), followlinks=True, is_ignored_dir=lambda entry: entry.name.startswith(".")
        )
        for entry in files
    ]

    assert walked == []


def test_a_non_recursive_following_walk_lists_every_link_beside_the_directory(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "shared").mkdir(parents=True)
    _link(root / "first", root / "shared")
    _link(root / "second", root / "shared")

    listed = [
        entry.name
        for _directory, subdirectories, _files in walk_dir_entries(str(root), followlinks=True, recursive=False)
        for entry in subdirectories
    ]

    assert sorted(listed) == ["first", "second", "shared"]


def test_a_following_walk_follows_a_link_to_a_directory_it_does_not_otherwise_reach(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.py").write_text("X = 1\n", encoding="utf-8")
    _link(root / "external", outside)

    assert _walked_files(root) == ["external/x.py"]


def test_a_trusted_projects_directory_scan_lists_each_file_once_over_a_link_to_its_root(looped_tree: Path) -> None:
    serena_config = SerenaConfig(
        gui_log_window=False,
        web_dashboard=False,
        log_level=logging.ERROR,
        language_backend=BuiltinLanguageBackend.LSP.get_instance(),
        trusted_project_path_patterns=[str(looped_tree)],
    )
    project_config = ProjectConfig(
        project_name="walk_cycle_test",
        language_servers=[LanguageServerId.PYTHON],
        language_backend=BuiltinLanguageBackend.LSP.get_instance(),
    )
    project = Project(project_root=str(looped_tree), project_config=project_config, serena_config=serena_config)
    assert project.is_trusted()

    scan = project.scan_directory("", recursive=True, skip_ignored_paths=False)

    assert [Path(f).as_posix() for f in scan.files if f.endswith(".py")] == ["pkg/module.py"]


def test_memories_behind_a_link_back_into_the_memories_folder_are_listed_once(tmp_path: Path) -> None:
    serena_folder = tmp_path / "project" / ".serena"
    manager = MemoryManager(serena_data_folder=serena_folder)
    memories = serena_folder / "memories"
    (memories / "topic").mkdir(parents=True)
    (memories / "topic" / "note.md").write_text("# note", encoding="utf-8")
    _link(memories / "topic" / "loop", memories)

    assert manager.list_memories().get_full_list() == ["topic/note"]
