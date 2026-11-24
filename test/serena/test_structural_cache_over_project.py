"""
The structural walks a session's :class:`CursorManager` keeps over a real project: of more files than
:attr:`CursorManager.MAX_STRUCTURAL_FILES`, the file used least recently is read again from the project when next used,
and a file used within the cap is not.
"""

import logging
import os
from pathlib import Path

from serena.config.serena_config import ProjectConfig, SerenaConfig
from serena.cursor import CursorManager
from serena.language_backend import BuiltinLanguageBackend
from serena.project import Project
from solidlsp.ls_config import LanguageServerId
from solidlsp.structural.backends.python import PythonStructuralLanguage
from solidlsp.structural.registry import StructuralBackendRegistry

_SOURCE = "class {name}:\n    def method(self) -> int:\n        return 1\n"
"""the source of each file, a class of the given name with one method"""


def _rewrite_keeping_mtime(path: Path, name: str) -> None:
    """
    Rewrites a file to hold a class of another name, keeping its modification time, so that only a fresh read of the file
    sees the change.

    :param path: the file
    :param name: the name of the class the file holds afterwards
    """
    stat = path.stat()
    path.write_text(_SOURCE.format(name=name), encoding="utf-8")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))


def test_a_session_reads_again_the_file_used_least_recently_beyond_the_cap_and_not_one_used_within_it(tmp_path: Path) -> None:
    # a project of one file more than the cap, each file holding the class Thing
    root = tmp_path / "project"
    root.mkdir()
    paths = [f"sample_{index}.py" for index in range(CursorManager.MAX_STRUCTURAL_FILES + 1)]
    for path in paths:
        (root / path).write_text(_SOURCE.format(name="Thing"), encoding="utf-8")
    serena_config = SerenaConfig(
        gui_log_window=False, web_dashboard=False, log_level=logging.ERROR, language_backend=BuiltinLanguageBackend.LSP.get_instance()
    )
    project = Project(
        project_root=str(root),
        project_config=ProjectConfig(
            project_name=root.name, language_servers=[LanguageServerId.PYTHON], language_backend=BuiltinLanguageBackend.LSP.get_instance()
        ),
        serena_config=serena_config,
    )
    registry = StructuralBackendRegistry()
    registry.register("python", [".py", ".pyi"], PythonStructuralLanguage)
    session = CursorManager(project, structural_registry=registry)
    for path in paths:
        assert session.resolve_structural_name_path(path, "Thing/method") is not None

    # the first file, used least recently, and the last, used within the cap, both changed with their times kept
    _rewrite_keeping_mtime(root / paths[0], "Other")
    _rewrite_keeping_mtime(root / paths[-1], "Other")

    assert session.resolve_structural_name_path(paths[0], "Other/method") is not None
    assert session.resolve_structural_name_path(paths[-1], "Thing/method") is not None
