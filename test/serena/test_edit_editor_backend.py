"""
Tests of the code editor a cursor edit tool builds for the agent's language backend through Component.create_code_editor:
whichever backend it is built for, the editor saves the edited file under the tool's write precondition
(pg://serena/edit/version, task://edit_atomicity.3) and atomically (pg://serena/edit/write, task://edit_atomicity.1), and the
JetBrains editor asks the IDE to refresh a file only once its save has landed.

The module imports nothing serena main lacks, so each of its tests of new behaviour runs against main's src, where it is
observed failing.
"""

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

import serena.tools.cursor_tools as cursor_tools
from serena.config.serena_config import LanguageBackend
from serena.cursor import CursorManager
from serena.jetbrains.jetbrains_plugin_client import JetBrainsPluginClient
from serena.tools.cursor_tools import CursorReplaceRangeTool
from serena.tools.tools_base import SUCCESS_RESULT
from serena.util.file_lifecycle import content_version
from solidlsp.ls_config import Language
from test.conftest import get_repo_path, project_with_ls_context
from test.serena.test_cursor_container_edit import _make_manager_via_subclass

STALE_MARKER = "changed under you"
"""the phrase the rendered stale state of a file carries"""

EDITED_FILE = "module.py"
"""the file of the JetBrains-backed project whose first line the edits replace"""

EDITED_SOURCE = "first = 1\nsecond = 2\n"
"""the contents of the file whose first line the JetBrains-backed edits replace"""

PYTHON_EDITED_FILE = os.path.join("test_repo", "variables.py")
"""the file of the Python test repository whose first line the language-server-backed edit replaces"""

REPLACED_LINE = "# the first line, replaced\n"
"""the line an edit puts in place of the edited file's first line"""

UNENCODABLE_LINE = "snowman = '☃'\n"
"""a line holding a snowman, a character the ascii codec cannot encode"""


class _ChangeAfterVersionCheck:
    """
    Another writer that appends a line to a file right after a cursor edit tool's version check has passed and before the
    tool writes, recording the file's bytes as it leaves them.
    """

    def __init__(self, path: Path) -> None:
        """
        :param path: the file the writer changes.
        """
        self._path = path
        self.bytes_after_change: bytes | None = None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        Follow every passing version check of the cursor edit tools with this writer's change.

        :param monkeypatch: the fixture through which the version check is wrapped.
        """
        original = cursor_tools._refuse_if_stale

        def refuse_if_stale(tool: Any, relative_path: str, expect_version: str) -> str | None:
            refusal = original(tool, relative_path, expect_version)
            if refusal is None:
                with open(self._path, "a", encoding="utf-8") as handle:
                    handle.write("concurrent = 1\n")
                self.bytes_after_change = self._path.read_bytes()
            return refusal

        monkeypatch.setattr(cursor_tools, "_refuse_if_stale", refuse_if_stale)


class _BackendAgent:
    """The agent a cursor edit tool reaches: its cursor manager, its active project, and the language backend it runs."""

    def __init__(self, manager: CursorManager, project: Any, backend: Any, using_language_server: bool) -> None:
        """
        :param manager: the agent's cursor manager.
        :param project: the agent's active project.
        :param backend: the language backend the agent runs.
        :param using_language_server: whether the agent is in language server mode.
        """
        self._manager = manager
        self._project = project
        self._backend = backend
        self._using_language_server = using_language_server

    def get_cursor_manager(self) -> CursorManager:
        return self._manager

    def get_active_project_or_raise(self) -> Any:
        return self._project

    def get_language_backend(self) -> Any:
        return self._backend

    def is_using_language_server(self) -> bool:
        return self._using_language_server


class _RecordingPluginClient:
    """The JetBrains plugin client a JetBrains code editor asks to refresh each file it saves; records each refresh."""

    def __init__(self) -> None:
        self.refreshed_paths: list[str] = []

    def __enter__(self) -> "_RecordingPluginClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def refresh_file(self, relative_path: str) -> None:
        self.refreshed_paths.append(relative_path)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        Make every JetBrains code editor reach this client, whatever project it edits.

        :param monkeypatch: the fixture through which the client lookup is replaced.
        """
        monkeypatch.setattr(JetBrainsPluginClient, "from_project", staticmethod(lambda project: self))


def _bind_replace_range(agent: _BackendAgent) -> CursorReplaceRangeTool:
    """
    :param agent: the agent the tool reaches.
    :return: a cursor_replace_range tool bound to ``agent``, whose code editor ``Component.create_code_editor`` builds.
    """
    tool = CursorReplaceRangeTool.__new__(CursorReplaceRangeTool)
    tool.__dict__["agent"] = agent
    return tool


def _jetbrains_tool(work_dir: Path, encoding: str = "utf-8") -> CursorReplaceRangeTool:
    """
    :param work_dir: the root of the edited project, into which the edited file is written.
    :param encoding: the project's encoding.
    :return: a cursor_replace_range tool whose agent runs the JetBrains backend over the project rooted at ``work_dir``.
    """
    # the edited file, and a project over it in the given encoding
    (work_dir / EDITED_FILE).write_text(EDITED_SOURCE, encoding="utf-8")
    manager = _make_manager_via_subclass(work_dir)
    project = manager._project  # type: ignore[attr-defined]
    project.project_config.encoding = encoding

    return _bind_replace_range(_BackendAgent(manager, project, LanguageBackend.JETBRAINS, using_language_server=False))


class TestJetBrainsEditorSave:
    """A line edit through the code editor built for the JetBrains backend."""

    def test_landed_save_is_refreshed_in_the_ide(self, work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression guard: a save that lands asks the IDE to refresh the saved file, once."""
        plugin_client = _RecordingPluginClient()
        plugin_client.install(monkeypatch)
        tool = _jetbrains_tool(work_dir)

        reply = tool.apply(relative_path=EDITED_FILE, start_line=1, end_line=1, body=REPLACED_LINE, expect_version="*")

        assert reply.startswith(SUCCESS_RESULT)
        assert (work_dir / EDITED_FILE).read_text(encoding="utf-8") == REPLACED_LINE + "second = 2\n"
        assert plugin_client.refreshed_paths == [EDITED_FILE]

    def test_change_after_version_check_is_kept_and_reported_stale(self, work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        plugin_client = _RecordingPluginClient()
        plugin_client.install(monkeypatch)
        tool = _jetbrains_tool(work_dir)
        edited_path = work_dir / EDITED_FILE

        # the version the caller read the file at, and another writer changing the file after the version check
        expect_version = content_version(edited_path.read_bytes())
        listing_before = sorted(os.listdir(work_dir))
        writer = _ChangeAfterVersionCheck(edited_path)
        writer.install(monkeypatch)

        reply = tool.apply(relative_path=EDITED_FILE, start_line=1, end_line=1, body=REPLACED_LINE, expect_version=expect_version)

        assert writer.bytes_after_change is not None
        assert edited_path.read_bytes() == writer.bytes_after_change
        assert sorted(os.listdir(work_dir)) == listing_before
        assert STALE_MARKER in reply
        assert plugin_client.refreshed_paths == []

    def test_unencodable_line_leaves_file_and_directory_and_is_not_refreshed(self, work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        plugin_client = _RecordingPluginClient()
        plugin_client.install(monkeypatch)
        tool = _jetbrains_tool(work_dir, encoding="ascii")
        edited_path = work_dir / EDITED_FILE
        bytes_before = edited_path.read_bytes()
        listing_before = sorted(os.listdir(work_dir))

        with pytest.raises(UnicodeEncodeError):
            tool.apply(relative_path=EDITED_FILE, start_line=1, end_line=1, body=UNENCODABLE_LINE, expect_version="*")

        assert edited_path.read_bytes() == bytes_before
        assert sorted(os.listdir(work_dir)) == listing_before
        assert plugin_client.refreshed_paths == []


@pytest.mark.python
class TestLanguageServerEditorSave:
    """A line edit through the code editor built for the language server backend."""

    def test_change_after_version_check_is_kept_and_reported_stale(self, work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # a copy of the Python test repository, and the version the caller read the edited file at
        repo_path = work_dir / get_repo_path(Language.PYTHON).name
        shutil.copytree(get_repo_path(Language.PYTHON), repo_path)
        edited_path = repo_path / PYTHON_EDITED_FILE
        expect_version = content_version(edited_path.read_bytes())
        listing_before = sorted(os.listdir(edited_path.parent))

        with project_with_ls_context(Language.PYTHON, str(repo_path)) as project:
            # a tool whose agent runs the language server backend, and another writer changing the file after the version check
            agent = _BackendAgent(CursorManager(project=project), project, LanguageBackend.LSP, using_language_server=True)
            tool = _bind_replace_range(agent)
            writer = _ChangeAfterVersionCheck(edited_path)
            writer.install(monkeypatch)

            reply = tool.apply(
                relative_path=PYTHON_EDITED_FILE, start_line=1, end_line=1, body=REPLACED_LINE, expect_version=expect_version
            )

            bytes_after = edited_path.read_bytes()
            listing_after = sorted(os.listdir(edited_path.parent))
        assert writer.bytes_after_change is not None
        assert bytes_after == writer.bytes_after_change
        assert listing_after == listing_before
        assert STALE_MARKER in reply


class TestCodeEditorFactory:
    """The code editor factories of a tool, asked for an editor they cannot build."""

    def test_unknown_backend_is_refused(self, work_dir: Path) -> None:
        """Regression guard: a language backend the factory does not know builds no editor."""
        manager = _make_manager_via_subclass(work_dir)
        tool = _bind_replace_range(_BackendAgent(manager, manager._project, backend=None, using_language_server=False))  # type: ignore[attr-defined]

        with pytest.raises(ValueError):
            tool.create_code_editor()

    def test_language_server_editor_outside_language_server_mode_is_refused(self, work_dir: Path) -> None:
        """Regression guard: an agent that is not in language server mode is given no language server code editor."""
        manager = _make_manager_via_subclass(work_dir)
        tool = _bind_replace_range(
            _BackendAgent(manager, manager._project, LanguageBackend.JETBRAINS, using_language_server=False)  # type: ignore[attr-defined]
        )

        with pytest.raises(Exception, match="not in language server mode"):
            tool.create_ls_code_editor()
