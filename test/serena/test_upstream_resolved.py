"""The tests of task://upstream_merge.7 that fail at main d69e440a and pass at upstream ede329aa, one per issue upstream resolves.

Their text is fact://serena/upstream_merge/issues_resolved's, as run on each side.
"""

import contextlib
import inspect
import threading
from types import SimpleNamespace

import pytest

from serena.agent import SerenaAgent
from serena.project import Project
from serena.task_executor import TaskExecutor
from serena.util.text_utils import search_files, search_text
from solidlsp.ls import SolidLanguageServer

LIVENESS_BOUND_SECONDS = 30.0
"""how long a test gives a thread before it opens the gate that thread may be blocked behind; no assertion reads a duration"""


def _matched_lines(matches) -> list[str]:
    return [line.line_content for match in matches for line in match.matched_lines]


def _search_text(pattern: str, content: str) -> list[str]:
    """Scans the whole text in one pass: main selects that scan with allow_multiline_match, which search_files passes; upstream always does."""
    multiline_scan = {"allow_multiline_match": True} if "allow_multiline_match" in inspect.signature(search_text).parameters else {}
    return _matched_lines(search_text(pattern, content=content, **multiline_scan))


def _search_files(root, relative_paths: list[str], pattern: str, paths_include_glob: str | None = None) -> list[str]:
    """Main takes paths and a root; upstream takes a FileCollection of file proxies."""
    if "file_collection" in inspect.signature(search_files).parameters:
        from serena.util.file_proxy import FileCollection, LocalProjectFileProxy

        project = SimpleNamespace(project_root=str(root), project_config=SimpleNamespace(encoding="utf-8"))
        files = FileCollection([LocalProjectFileProxy(path, project) for path in relative_paths])
        return _matched_lines(search_files(files, pattern, paths_include_glob=paths_include_glob))
    return _matched_lines(search_files(relative_paths, pattern, root_path=str(root), paths_include_glob=paths_include_glob))


def _write(root, relative_path: str, content: str) -> str:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return relative_path


# bug://serena/search-for-pattern-caret-anchors-to-file-start-not-line-start
def test_caret_anchors_to_line_start() -> None:
    assert _search_text("^def", "x = 1\ndef f():\n    pass\n") == ["def f():"]


# symptom://serena/cursor/grep/multiline/flag, with the pattern the symptom recorded
def test_inline_multiline_flag_with_recorded_pattern_returns_every_line(tmp_path) -> None:
    path = _write(tmp_path, "offer.js", "first\nsecond\n")
    matched = _search_files(tmp_path, [path], r"(?m)^[^\n]*$")
    assert "first" in matched and "second" in matched


# bug://serena/search/failure/absence/indistinguishability
def test_invalid_regular_expression_raises_and_names_the_pattern(tmp_path) -> None:
    path = _write(tmp_path, "a.py", "x = 1\n")
    with pytest.raises(Exception) as raised:
        _search_files(tmp_path, [path], "(unclosed")
    assert "(unclosed" in str(raised.value)


# bug://serena/executor/task/result/timeout
def test_execute_task_timeout_returns_while_task_is_held_on_gate() -> None:
    executor = TaskExecutor("t7")
    gate = threading.Event()
    started = threading.Event()
    returned = threading.Event()
    outcome: dict[str, object] = {}

    def held_task() -> str:
        started.set()
        gate.wait()
        return "released"

    def caller() -> None:
        try:
            outcome["result"] = executor.execute_task(held_task, timeout=2.0)
        except BaseException as e:
            outcome["result"] = e
        outcome["gate_open_when_caller_returned"] = gate.is_set()
        returned.set()

    threading.Thread(target=caller, daemon=True).start()
    try:
        returned.wait(LIVENESS_BOUND_SECONDS)
    finally:
        gate.set()
    assert returned.wait(LIVENESS_BOUND_SECONDS), "execute_task did not return even after the gate opened"
    assert started.is_set(), "the deadline passed before the task started, so nothing was held on the gate"
    assert outcome["gate_open_when_caller_returned"] is False, f"execute_task returned only once the gate opened: {outcome['result']!r}"
    assert isinstance(outcome["result"], TimeoutError), repr(outcome["result"])


# bug://serena/language_server/file/buffer/reference/release
def test_open_file_whose_body_raises_leaves_no_buffer(tmp_path) -> None:
    class BareServer(SolidLanguageServer):
        def _get_language_id_for_file(self, relative_file_path: str) -> str:
            return "python"

    # only open_file runs, so the abstract methods, which differ between the sides, stay unimplemented
    BareServer.__abstractmethods__ = frozenset()
    server = BareServer.__new__(BareServer)
    server.server_started = True
    server.repository_root_path = str(tmp_path)
    server.open_file_buffers = {}
    server._encoding = "utf-8"
    _write(tmp_path, "a.py", "x = 1\n")

    class BodyFailure(Exception):
        pass

    with pytest.raises(BodyFailure):
        with server.open_file("a.py", open_in_ls=False):
            raise BodyFailure

    assert server.open_file_buffers == {}


# bug://serena/project/ignore/spec/readiness, for an Exception; a BaseException that is not one stays open at both sides
@pytest.mark.parametrize("failure", [RuntimeError("gather failed")], ids=["exception"])
def test_gather_failure_leaves_no_reader_waiting(failure: BaseException) -> None:
    class RaisingConfig:
        @property
        def ignored_paths(self) -> list[str]:
            raise failure

    project = Project.__new__(Project)
    project.project_config = SimpleNamespace(project_name="t7", ignored_paths=[])
    project.serena_config = RaisingConfig()
    project._ignore_spec_available = threading.Event()

    # the thread body runs on this thread, so its return is the ordering point and no clock is involved
    with contextlib.suppress(Exception):
        project._gather_ignorespec()

    # a read blocks exactly when this event is unset, so checking it first keeps the test from blocking on the defect
    assert project._ignore_spec_available.is_set(), "a reader of the ignore spec would wait forever"
    with contextlib.suppress(Exception):
        _ = project._ignore_spec


class _InitFailure(Exception):
    pass


class _PartialAgent(SerenaAgent):
    def __del__(self) -> None:
        # the test calls SerenaAgent.__del__ itself; the collector must not call it again
        pass


class _AgentFailingAtFirstAssignment(_PartialAgent):
    def __setattr__(self, name: str, value: object) -> None:
        raise _InitFailure(name)


# bug://serena/agent/shutdown/finalizer/partial_init
def test_del_after_init_raised_at_its_first_assignment_raises_nothing() -> None:
    agent = _AgentFailingAtFirstAssignment.__new__(_AgentFailingAtFirstAssignment)
    with pytest.raises(_InitFailure):
        agent.__init__()
    SerenaAgent.__del__(agent)
