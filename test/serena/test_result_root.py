"""
Tests that every cursor view and every write result names the absolute project root its path was resolved against, on a
line of its own reading ``root: <absolute path>``: the line ``hooks/track-serena-cursor-path.sh`` in brain records as the
session's serena root, and the one thing in a result that says which tree answered (task://issue_bin_closure_result_root.1).

Both projects of a test hold a file of one relative path, so nothing but the root line tells their answers apart. A result is
read at :meth:`Tool.apply_ex`, the boundary every published tool call passes, and its root the way the hook reads it (see
:data:`_ROOT_LINE`): the first matching line of the result, whatever the result goes on to render.

The readers outside the cursor surface are not named here: bug://serena/result/root/reader/absence lists them.
"""

import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from serena.agent import _PIPE_SESSION_ID_VAR, SerenaAgent
from serena.config.serena_config import ProjectConfig, RegisteredProject, SerenaConfig
from serena.project import Project
from serena.tools.config_tools import ActivateProjectTool
from serena.tools.cursor_tools import (
    CursorConfigureTool,
    CursorFindTool,
    CursorGrepTool,
    CursorHistoryTool,
    CursorInsertAfterTool,
    CursorLookTool,
    CursorMoveTool,
    CursorNarrateTool,
    CursorOverviewTool,
    CursorReplaceRangeVerifiedTool,
    CursorStartTool,
)
from serena.tools.file_tools import CreateTextFileTool
from serena.tools.tools_base import SUCCESS_RESULT, Tool, ToolMarkerCanEdit
from solidlsp.ls_config import Language
from test.conftest import language_tests_enabled
from test.serena.test_streamable_http_cc_session_key import _make_mcp_ctx

_SESSION = "cc-session-result-root"
"""the forwarded session id of every call of a test"""

_OTHER_SESSION = "cc-session-result-root-other"
"""the forwarded session id of a session other than the test's"""

_BECAUSE = "to learn which tree answers"
"""the stated goal of every call that requires one"""

_ROOT_LINE = re.compile(r"^root: (/.*\S)\s*$", re.MULTILINE)
"""
the line ``hooks/track-serena-cursor-path.sh`` in brain records a root from: its ``VIEW_ROOT`` is the ``sed`` match of this form
piped through ``head -n1``, so the first such line of a result is the one recorded
"""

_EDITED_FILE = "module.py"
"""the relative path of the file both projects of a test hold"""

_REPLACED_LINE = "        return 'hello'"
"""the third line of the edited file"""

_SOURCE = f"class Greeter:\n    def greet(self):\n{_REPLACED_LINE}\n"
"""the content of the edited file"""

_ROOT_CARRYING_FILE = "settings.yaml"
"""the relative path of a file whose first line reads as a root line"""

_ELSEWHERE = "/elsewhere"
"""the root the root-carrying file's first line names"""

_HOLD_BOUND_SECONDS = 30.0
"""the bound on the wait for a held write to start, which only a failing test reaches"""

_DEADLINE_SECONDS = 2.0
"""the deadline a held write is abandoned at, which is only rendered: no test waits it out"""


def _root_named_by(result: str) -> str | None:
    """
    :param result: the result of a tool call
    :return: the root the hook records from the result, or None if it records none
    """
    match = _ROOT_LINE.search(result)
    return match.group(1) if match is not None else None


def _project_at(root: Path, serena_config: SerenaConfig) -> Project:
    """
    :param root: a directory to create, holding the edited file
    :param serena_config: the configuration of the agent that will hold the project
    :return: a project rooted at the directory
    """
    root.mkdir()
    (root / _EDITED_FILE).write_text(_SOURCE, encoding="utf-8")
    return Project(
        project_root=str(root.resolve()),
        project_config=ProjectConfig(
            project_name=root.name,
            languages=[Language.PYTHON],
            ignored_paths=[],
            excluded_tools=[],
            read_only=False,
            ignore_all_files_in_gitignore=True,
            initial_prompt="",
            encoding="utf-8",
        ),
        serena_config=serena_config,
    )


@dataclass
class _Session:
    """One client session of an agent, whose calls are all issued at the published boundary under one forwarded session id."""

    agent: SerenaAgent
    first_root: str
    second_root: str

    def call(self, tool_class: type[Tool], **arguments: Any) -> str:
        """
        :param tool_class: the class of the tool to call
        :param arguments: the arguments of the call
        :return: the result of the call
        """
        return self.agent.get_tool(tool_class).apply_ex(mcp_ctx=_make_mcp_ctx(forwarded_header=_SESSION), log_call=False, **arguments)

    def activate(self, root: str) -> None:
        """
        :param root: the root of the registered project to activate for the session
        """
        self.call(ActivateProjectTool, project=root)

    def activate_in_another_session(self, root: str) -> None:
        """
        :param root: the root of the registered project to activate for a session other than this one
        """
        self.agent.get_tool(ActivateProjectTool).apply_ex(
            mcp_ctx=_make_mcp_ctx(forwarded_header=_OTHER_SESSION), log_call=False, project=root
        )

    def start_cursor(self, name_path: str = "Greeter", relative_path: str = _EDITED_FILE) -> str:
        """
        :param name_path: the name path of the symbol to open a cursor on
        :param relative_path: the file of the symbol
        :return: the id of the opened cursor
        """
        view = self.call(CursorStartTool, name_path=name_path, relative_path=relative_path, because=_BECAUSE, edge_types=["contains"])
        match = re.search(r"Started cursor (\S+)\.", view)
        assert match is not None, f"no cursor was opened on {name_path!r}: {view}"
        return match.group(1)


@pytest.fixture(autouse=True)
def _no_pipe_session() -> Iterator[None]:
    """Keeps the pipe transport's session id out of scope, so the forwarded session id keys every call."""
    token = _PIPE_SESSION_ID_VAR.set(None)
    yield
    _PIPE_SESSION_ID_VAR.reset(token)


@pytest.fixture
def session(work_dir: Path) -> Iterator[_Session]:
    """A session of a real agent that holds no project yet, with two registered projects to activate."""
    if not language_tests_enabled(Language.PYTHON):
        pytest.skip("Python tests not enabled")

    config = SerenaConfig(gui_log_window=False, web_dashboard=False)
    first, second = _project_at(work_dir / "first", config), _project_at(work_dir / "second", config)
    config.projects = [RegisteredProject.from_project_instance(project) for project in (first, second)]
    agent = SerenaAgent(serena_config=config)
    yield _Session(agent=agent, first_root=first.project_root, second_root=second.project_root)
    agent.on_shutdown(timeout=5)


class TestCursorViewsNameTheRoot:
    """every cursor view names the absolute root of the project the session holds"""

    def test_views_of_the_cursor_readers_name_the_active_root(self, session: _Session) -> None:
        """Each reader of the cursor surface answers with the root of the project the session activated."""
        session.activate(session.first_root)
        cursor_id = session.start_cursor()

        views = {
            "cursor_start": session.call(CursorStartTool, name_path="Greeter", relative_path=_EDITED_FILE, because=_BECAUSE),
            "cursor_find": session.call(CursorFindTool, name_path_pattern="Greeter", relative_path=_EDITED_FILE, because=_BECAUSE),
            "cursor_grep": session.call(CursorGrepTool, substring_pattern="hello", relative_path=_EDITED_FILE, because=_BECAUSE),
            "cursor_overview": session.call(CursorOverviewTool, relative_path=_EDITED_FILE, because=_BECAUSE),
            "cursor_look": session.call(CursorLookTool, cursor_id=cursor_id),
            "cursor_configure": session.call(CursorConfigureTool, cursor_id=cursor_id, edge_types=["contains"], include_body=True),
            "cursor_narrate": session.call(CursorNarrateTool, cursor_id=cursor_id, because=_BECAUSE),
            "cursor_move": session.call(CursorMoveTool, cursor_id=cursor_id, target_name="greet", because=_BECAUSE),
            "cursor_history": session.call(CursorHistoryTool, cursor_id=cursor_id),
        }

        for tool_name, view in views.items():
            assert _root_named_by(view) == session.first_root, f"the view of {tool_name} does not name the active root: {view}"

    def test_a_report_of_absence_names_the_tree_it_was_measured_in(self, session: _Session) -> None:
        """A search that finds nothing and a read of a missing file each say which tree they looked in."""
        session.activate(session.first_root)

        reports = {
            "cursor_grep": session.call(CursorGrepTool, substring_pattern="not_in_this_tree", relative_path=_EDITED_FILE, because=_BECAUSE),
            "cursor_find": session.call(CursorFindTool, name_path_pattern="NotInThisTree", relative_path=_EDITED_FILE, because=_BECAUSE),
            "cursor_overview": session.call(CursorOverviewTool, relative_path="not_in_this_tree.py", because=_BECAUSE),
        }

        for tool_name, report in reports.items():
            assert _root_named_by(report) == session.first_root, f"the report of {tool_name} does not name the active root: {report}"

    def test_a_body_that_reads_as_a_root_line_does_not_shadow_the_root(self, session: _Session) -> None:
        """
        A structural body renders its lines unnumbered, so a file's own ``root: <path>`` line reaches a view looking exactly
        like the root line. The hook keeps the first such line of a result (see :data:`_ROOT_LINE`), so the root line precedes
        the body.
        """
        session.activate(session.first_root)
        Path(session.first_root, _ROOT_CARRYING_FILE).write_text(f"root: {_ELSEWHERE}\n", encoding="utf-8")
        cursor_id = session.start_cursor(name_path="root", relative_path=_ROOT_CARRYING_FILE)

        view = session.call(CursorConfigureTool, cursor_id=cursor_id, include_body=True)

        assert f"root: {_ELSEWHERE}" in view, f"the view does not render the file's own line: {view}"
        assert _root_named_by(view) == session.first_root, f"the file's own line shadowed the root: {view}"


class TestWriteResultsNameTheRoot:
    """every write result names the absolute root of the project the write resolved against"""

    def test_write_results_name_the_active_root(self, session: _Session) -> None:
        """A range writer, a file creation and a cursor writer each answer with the root the write landed under."""
        session.activate(session.first_root)
        cursor_id = session.start_cursor()

        results = {
            "cursor_replace_range_verified": session.call(
                CursorReplaceRangeVerifiedTool,
                relative_path=_EDITED_FILE,
                start_line=3,
                end_line=3,
                expected_content=_REPLACED_LINE,
                body="        return 'replaced'",
                expect_version="*",
            ),
            "create_text_file": session.call(CreateTextFileTool, relative_path="created.txt", content="created\n"),
            "cursor_insert_after": session.call(CursorInsertAfterTool, cursor_id=cursor_id, body="\n\ninserted = 1\n", expect_version="*"),
        }

        for tool_name, result in results.items():
            assert _root_named_by(result) == session.first_root, f"the result of {tool_name} does not name the active root: {result}"

        # the writes landed under the root the results named, and under no other
        edited = Path(session.first_root, _EDITED_FILE).read_text(encoding="utf-8")
        assert "replaced" in edited and "inserted" in edited, f"the edits did not land under the named root: {edited}"
        assert Path(session.first_root, "created.txt").is_file(), "the file was not created under the named root"
        assert Path(session.second_root, _EDITED_FILE).read_text(encoding="utf-8") == _SOURCE, "a write landed under the other project"
        assert not Path(session.second_root, "created.txt").exists(), "the file was created under the other project"

    def test_a_refused_write_names_the_tree_that_refused_it(self, session: _Session) -> None:
        """A write refused on the content it found says which tree held that content, and leaves the file as it was."""
        session.activate(session.first_root)

        result = session.call(
            CursorReplaceRangeVerifiedTool,
            relative_path=_EDITED_FILE,
            start_line=3,
            end_line=3,
            expected_content="        return 'content of another tree'",
            body="        return 'replaced'",
            expect_version="*",
        )

        assert _root_named_by(result) == session.first_root, f"the refusal does not name the active root: {result}"
        assert Path(session.first_root, _EDITED_FILE).read_text(encoding="utf-8") == _SOURCE, "the refused write changed the file"


class _HeldWriteTool(Tool, ToolMarkerCanEdit):
    """A write whose call holds until the test releases it, as a write past its deadline does."""

    def __init__(self, agent: SerenaAgent) -> None:
        super().__init__(agent)
        self.entered = threading.Event()
        self.release = threading.Event()

    def apply(self) -> str:
        """
        Holds until the test releases it.

        :return: the success result of a write, once released
        """
        self.entered.set()
        self.release.wait()
        return SUCCESS_RESULT


class TestWritePastItsDeadlineNamesTheRoot:
    """the answer to a write past its deadline is given outside the call's task, and the write can still land"""

    def test_a_write_past_its_deadline_names_the_root_of_its_own_call(self, session: _Session, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        The thread that answers at the deadline holds none of the call's bindings and another session holds the second
        project, so the root can only come from the call's own context. The abandonment is called with the write held on a
        gate, which fixes the order without a clock.
        """
        session.activate(session.first_root)
        session.activate_in_another_session(session.second_root)
        write = _HeldWriteTool(session.agent)
        monkeypatch.setattr(write, "is_active", lambda: True)

        task_exec = write._issue_call(False, True, _make_mcp_ctx(forwarded_header=_SESSION))
        try:
            assert write.entered.wait(_HOLD_BOUND_SECONDS), "the write never started"
            answer = write._timeout_result(task_exec, _DEADLINE_SECONDS)
        finally:
            write.release.set()

        assert _root_named_by(answer) == session.first_root, f"the answer does not name the root of its call: {answer}"
        assert "timed out" in answer


class TestActivationMovesTheRoot:
    """the root a result names follows the session's activation"""

    def test_the_view_after_a_second_activation_names_the_second_root(self, session: _Session) -> None:
        """One relative path answered by two projects in one session: the next view names the second project's root."""
        session.activate(session.first_root)
        first_view = session.call(CursorOverviewTool, relative_path=_EDITED_FILE, because=_BECAUSE)
        assert _root_named_by(first_view) == session.first_root, f"the first view does not name the first root: {first_view}"

        session.activate(session.second_root)
        second_view = session.call(CursorOverviewTool, relative_path=_EDITED_FILE, because=_BECAUSE)

        assert _root_named_by(second_view) == session.second_root, f"the next view does not name the second root: {second_view}"
