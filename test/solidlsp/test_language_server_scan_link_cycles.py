"""
The project scans language servers make of their own, over a tree whose directory links loop back into it: each scan
reaches every file once and ends.
"""

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


def test_the_build_root_search_finds_each_build_root_once_over_a_link_to_the_root(looped_tree: Path) -> None:
    (looped_tree / "pkg" / "build.sbt").write_text("", encoding="utf-8")

    assert find_build_roots(str(looped_tree)) == [str(looped_tree / "pkg")]
