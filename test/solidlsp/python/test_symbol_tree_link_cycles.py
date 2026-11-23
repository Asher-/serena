"""Symbol discovery in a trusted project whose directory links lead back into the project, and the release of a stopped
server's symbols.

a trusted project's symbol tree holds each file once however its links loop or alias directories, so the symbols
cached for the project are bounded by its files; and a stopped server keeps none of the symbols it served alive.
"""

import gc
import weakref
from dataclasses import replace
from pathlib import Path

import pytest

from solidlsp import ls_types
from solidlsp.ls_config import LanguageServerId
from test.conftest import start_ls_context

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

    assert len(paths) == 3
    assert {"main.py", "pkg/module.py"} <= set(paths)
    assert len([p for p in paths if p.endswith("linked.py")]) == 1


def test_a_stopped_server_keeps_none_of_the_symbols_it_served_alive(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text("def ordinary():\n    return 1\n", encoding="utf-8")

    with start_ls_context(ls_id=LanguageServerId.PYTHON, repo_path=str(root)) as ls:
        served = weakref.ref(ls.request_document_symbols("main.py"))
        ls.stop()
        gc.collect()

        assert served() is None


def test_a_server_started_again_after_a_stop_serves_the_symbols_saved_before_from_its_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text("def ordinary():\n    return 1\n", encoding="utf-8")

    with start_ls_context(ls_id=LanguageServerId.PYTHON, repo_path=str(root)) as ls:
        ls.request_document_symbols("main.py")
        ls.save_cache()
        ls.stop()
        ls.start()

        def unreachable(*_arguments: object, **_keyword_arguments: object) -> None:
            raise AssertionError("the symbols were requested from the language server instead of the cache")

        monkeypatch.setattr(ls, "_request_raw_document_symbols", unreachable)
        names = [symbol["name"] for symbol in ls.request_document_symbols("main.py").root_symbols]

    assert names == ["ordinary"]
