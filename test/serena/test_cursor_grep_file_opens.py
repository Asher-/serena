"""
A cursor grep with many matches in one file opens that file on its language server once, not once per match.
"""

from typing import Any

import pytest

from serena.agent import SerenaAgent

pytestmark = pytest.mark.python

_FILE = "test_repo/services.py"


def test_a_grep_with_many_matches_in_one_file_opens_that_file_on_the_language_server_once(
    python_serena_agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = python_serena_agent
    notify = agent.get_language_server_manager_or_raise().get_language_server(_FILE).server.notify
    opened: list[str] = []
    open_document = notify.did_open_text_document

    def counted_open_document(params: Any) -> None:
        opened.append(params["textDocument"]["uri"])
        open_document(params)

    monkeypatch.setattr(notify, "did_open_text_document", counted_open_document)

    groups, _ = agent.execute_task(lambda: agent.get_cursor_manager().find_pattern_with_enclosing_symbols("self", relative_path=_FILE))

    assert sum(len(hits) for _, hits in groups) > 1
    assert [uri for uri in opened if uri.endswith(_FILE)] == [opened[0]]
