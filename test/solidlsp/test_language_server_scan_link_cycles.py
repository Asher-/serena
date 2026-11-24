"""
The project scans language servers make of their own, over a tree whose directory links loop back into it: each scan
reaches every file once and ends, and follows a link to a directory it does not enter otherwise.
"""

import os
from pathlib import Path

import pytest

from solidlsp.language_servers import csharp_language_server, omnisharp, vbnet_language_server
from solidlsp.language_servers.scala_language_server import find_build_roots


@pytest.fixture
def looped_tree(tmp_path: Path) -> Path:
    """
    :return: a tree holding ``pkg/source.cs`` and ``pkg/loop``, a link to the tree's root
    """
    root = tmp_path / "root"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "source.cs").write_text("class C {}\n", encoding="utf-8")
    try:
        (root / "pkg" / "loop").symlink_to(root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symbolic links are unavailable: {exc}")
    return root


@pytest.mark.parametrize(
    "scan",
    [csharp_language_server.breadth_first_file_scan, vbnet_language_server.breadth_first_file_scan, omnisharp.breadth_first_file_scan],
)
def test_a_breadth_first_scan_yields_each_file_once_over_a_link_to_the_root(looped_tree: Path, scan) -> None:
    files = [Path(path).relative_to(looped_tree).as_posix() for path in scan(str(looped_tree))]

    assert files == ["pkg/source.cs"]


@pytest.mark.parametrize(
    "scan",
    [csharp_language_server.breadth_first_file_scan, vbnet_language_server.breadth_first_file_scan, omnisharp.breadth_first_file_scan],
)
def test_a_breadth_first_scan_yields_the_files_of_a_directory_two_links_alias_once(tmp_path: Path, scan) -> None:
    # a directory outside the scanned tree, which two links in it point to
    root = tmp_path / "root"
    root.mkdir()
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "source.cs").write_text("class C {}\n", encoding="utf-8")
    try:
        (root / "first").symlink_to(shared, target_is_directory=True)
        (root / "second").symlink_to(shared, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symbolic links are unavailable: {exc}")

    files = [Path(path).relative_to(root).as_posix() for path in scan(str(root))]

    assert files == ["first/source.cs"]


def test_the_build_root_search_finds_a_build_root_two_links_alias_once(tmp_path: Path) -> None:
    # a build root outside the searched tree, which two links in it point to
    root = tmp_path / "root"
    root.mkdir()
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "build.sbt").write_text("", encoding="utf-8")
    try:
        (root / "first").symlink_to(shared, target_is_directory=True)
        (root / "second").symlink_to(shared, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symbolic links are unavailable: {exc}")

    assert find_build_roots(str(root)) == [str(root / "first")]


def test_the_build_root_search_finds_each_build_root_once_over_a_link_to_the_root(looped_tree: Path) -> None:
    (looped_tree / "pkg" / "build.sbt").write_text("", encoding="utf-8")

    assert find_build_roots(str(looped_tree)) == [str(looped_tree / "pkg")]


@pytest.mark.parametrize("hidden_build_root", ["src/app", "a/b/c/app"], ids=["inside_a_skipped_directory", "below_the_depth_searched"])
def test_the_build_root_search_finds_a_build_root_it_reaches_only_through_a_link_once(tmp_path: Path, hidden_build_root: str) -> None:
    # a build root the search never enters by real directories, and a link to it beside the root's other entries
    root = tmp_path / "root"
    (root / hidden_build_root).mkdir(parents=True)
    (root / hidden_build_root / "build.sbt").write_text("", encoding="utf-8")
    try:
        (root / "linked").symlink_to(root / hidden_build_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symbolic links are unavailable: {exc}")

    assert find_build_roots(str(root)) == [str(root / "linked")]


def test_the_solidity_file_count_counts_each_file_once_over_a_link_to_the_root(looped_tree: Path) -> None:
    (looped_tree / "token.sol").write_text("contract Token {}\n", encoding="utf-8")
    (looped_tree / "pkg" / "vault.sol").write_text("contract Vault {}\n", encoding="utf-8")
    # imported here, so that the module's other tests run against a source without it
    from solidlsp.language_servers.solidity_language_server import count_solidity_files

    assert count_solidity_files(str(looped_tree)) == 2


@pytest.mark.timeout(30)
def test_the_tsgo_executable_search_ends_and_finds_the_executable_over_links_that_loop_in_node_modules(tmp_path: Path) -> None:
    # node_modules holding the executable and two links back to itself, which double the paths at every level
    node_modules = tmp_path / "install" / "node_modules"
    executable_dir = node_modules / "@typescript" / "typescript-native" / "lib"
    executable_dir.mkdir(parents=True)
    executable_name = "tsc.exe" if os.name == "nt" else "tsc"
    (executable_dir / executable_name).write_text("", encoding="utf-8")
    try:
        (node_modules / "again").symlink_to(node_modules, target_is_directory=True)
        (node_modules / "loop").symlink_to(node_modules, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symbolic links are unavailable: {exc}")

    # imported here, so that the module's other tests run against a source without it
    from solidlsp.language_servers.tsgo_language_server import find_tsgo_executable

    found = find_tsgo_executable(str(tmp_path / "install"))

    assert found == str(executable_dir / executable_name)
