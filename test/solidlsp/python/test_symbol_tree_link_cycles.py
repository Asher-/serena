"""Symbol discovery in a trusted project whose directory links lead back into the project, and the release of a stopped
server's symbols.

a trusted project's symbol tree holds each file once however its links loop or alias directories, so the symbols
cached for the project are bounded by its files; and a stopped server keeps none of the saved symbols it served alive.
"""

import gc
import logging
import weakref
from dataclasses import replace
from pathlib import Path

import psutil
import pytest

from solidlsp import ls_types
from solidlsp.ls_config import LanguageServerId
from solidlsp.util.cache import load_pickle
from test.conftest import _create_ls, start_ls_context

pytestmark = pytest.mark.python


def _link(link: Path, target: Path) -> None:
    """
    Creates a symbolic link to a directory, skipping the test where the platform refuses one.

    :param link: the path of the link
    :param target: the directory the link points to
    """
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symbolic links are unavailable: {exc}")


def _file_paths(nodes: list[ls_types.UnifiedSymbolInformation]) -> list[str]:
    """
    :param nodes: symbols of a full symbol tree
    :return: the relative path of every file symbol in the tree, one entry per occurrence
    """
    paths = []
    for node in nodes:
        if node["kind"] == ls_types.SymbolKind.File:
            paths.append(Path(node["location"]["relativePath"]).as_posix())
        paths.extend(_file_paths(node.get("children", [])))
    return paths


def test_a_trusted_projects_symbol_tree_holds_each_file_once_over_looping_and_aliasing_links(tmp_path: Path) -> None:
    # a project whose package links back to the root and to itself, and two links to one outside directory
    root = tmp_path / "project"
    (root / "pkg").mkdir(parents=True)
    (root / "main.py").write_text("def ordinary():\n    return 1\n", encoding="utf-8")
    (root / "pkg" / "module.py").write_text("def nested():\n    return 2\n", encoding="utf-8")
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "linked.py").write_text("def linked():\n    return 3\n", encoding="utf-8")
    _link(root / "pkg" / "loop", root)
    _link(root / "pkg" / "self", root / "pkg")
    _link(root / "first", shared)
    _link(root / "second", shared)

    with start_ls_context(ls_id=LanguageServerId.PYTHON, repo_path=str(root)) as ls:
        ls.config = replace(ls.config, is_project_trusted=True)
        paths = _file_paths(ls.request_full_symbol_tree())
        # the document symbols the server holds, as its public save writes them
        ls.save_cache()
        saved = load_pickle(str(ls.cache_dir / ls.DOCUMENT_SYMBOL_CACHE_FILENAME))["obj"]

    assert len(paths) == 3
    assert {"main.py", "pkg/module.py"} <= set(paths)
    assert len([p for p in paths if p.endswith("linked.py")]) == 1
    # at most one entry per distinct real file: main.py, pkg/module.py and the shared linked.py
    assert 0 < len(saved) <= 3


@pytest.mark.parametrize("alias_inside_project", [False, True], ids=["alias_outside_the_project", "alias_inside_the_project"])
def test_a_symbol_tree_over_two_workspace_roots_with_one_real_directory_holds_each_file_once(
    tmp_path: Path, alias_inside_project: bool
) -> None:
    # a guard of the claim that a second root sharing the first's real directory adds no file: the outside alias is an
    # ignored path, and the inside alias is a link whose real directory, the root, is entered already
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text("def ordinary():\n    return 1\n", encoding="utf-8")
    alias = (root if alias_inside_project else tmp_path) / "alias"
    _link(alias, root)

    with start_ls_context(ls_id=LanguageServerId.PYTHON, repo_path=str(root)) as ls:
        ls.config = replace(ls.config, is_project_trusted=True, workspace_folders=[str(root), str(alias)])
        paths = _file_paths(ls.request_full_symbol_tree())

    assert paths == ["main.py"]


def test_a_stopped_server_keeps_none_of_the_saved_symbols_it_served_alive(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text("def ordinary():\n    return 1\n", encoding="utf-8")

    with start_ls_context(ls_id=LanguageServerId.PYTHON, repo_path=str(root)) as ls:
        served = weakref.ref(ls.request_document_symbols("main.py"))
        ls.save_cache()
        ls.stop()
        gc.collect()

        assert served() is None


def test_a_stop_keeps_the_unsaved_symbols_a_server_served_for_a_later_save(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text("def ordinary():\n    return 1\n", encoding="utf-8")

    with start_ls_context(ls_id=LanguageServerId.PYTHON, repo_path=str(root)) as ls:
        ls.request_document_symbols("main.py")
        ls.stop()
        ls.save_cache()
        saved = load_pickle(str(ls.cache_dir / ls.DOCUMENT_SYMBOL_CACHE_FILENAME))["obj"]

    assert list(saved) == ["main.py"]


def test_a_save_whose_cache_files_cannot_be_written_logs_the_failure_and_raises_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text("def ordinary():\n    return 1\n", encoding="utf-8")

    with start_ls_context(ls_id=LanguageServerId.PYTHON, repo_path=str(root)) as ls:
        ls.request_document_symbols("main.py")
        # directories where the cache files would be written, so that neither write can succeed
        (ls.cache_dir / ls.RAW_DOCUMENT_SYMBOL_CACHE_FILENAME).mkdir(parents=True, exist_ok=True)
        (ls.cache_dir / ls.DOCUMENT_SYMBOL_CACHE_FILENAME).mkdir(parents=True, exist_ok=True)

        with caplog.at_level(logging.ERROR):
            ls.save_cache()

    assert "Failed to save raw document symbols cache" in caplog.text
    assert "Failed to save document symbols cache" in caplog.text


def test_a_server_names_the_process_it_started_and_none_before_it_starts(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text("def ordinary():\n    return 1\n", encoding="utf-8")

    assert _create_ls(LanguageServerId.PYTHON, repo_path=str(root)).process_id() is None
    with start_ls_context(ls_id=LanguageServerId.PYTHON, repo_path=str(root)) as ls:
        pid = ls.process_id()

        assert pid is not None
        assert psutil.pid_exists(pid)


def test_a_server_started_again_after_a_stop_serves_the_symbols_of_its_files(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text("def ordinary():\n    return 1\n", encoding="utf-8")

    with start_ls_context(ls_id=LanguageServerId.PYTHON, repo_path=str(root)) as ls:
        ls.request_document_symbols("main.py")
        ls.save_cache()
        ls.stop()
        ls.start()
        names = [symbol["name"] for symbol in ls.request_document_symbols("main.py").root_symbols]

    assert names == ["ordinary"]
