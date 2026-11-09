"""
The call journal keeps each entry at <owner>/entry/<id>.json and reads it only by its id, removes the journal of the
layout before it whole by its known path, and retires an answered entry (task://retention_keyed_stores.8).
"""

import ast
import json
from pathlib import Path
from typing import Any, cast

import pytest
from starlette.testclient import TestClient

from serena import call_journal
from serena.call_journal import CallJournal, CallJournalLayer, CallResult, NoCallError
from solidlsp.util import supervise


@pytest.fixture(autouse=True)
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(supervise.ROOT_ENV, str(tmp_path))
    return tmp_path


def _owner() -> Path:
    return supervise.restart_root() / supervise.OWNER


def test_every_call_is_read_by_its_id_in_an_entry_directory_that_cannot_be_listed() -> None:
    journal = CallJournal()
    for call in ("a", "b"):
        assert journal.receive(call, "t", {})
        journal.finish(call, CallResult(f"answer {call}", False))
    entries = _owner() / "entry"
    (entries / "b.json").chmod(0o000)
    entries.chmod(0o300)
    try:
        assert journal.receive("c", "t", {})
        journal.finish("c", CallResult("answer c", False))
        assert journal.resume("c").text == "answer c"
        journal.retire("c")
        with pytest.raises(NoCallError):
            journal.resume("c")
    finally:
        entries.chmod(0o755)
        (entries / "b.json").chmod(0o644)
        journal.close()


def test_the_journal_of_the_layout_before_is_removed_whole_without_reading_an_entry() -> None:
    legacy = _owner() / "call"
    legacy.mkdir(parents=True)
    for i in range(200):
        entry = legacy / f"c{i}.json"
        entry.write_text(json.dumps({"id": f"c{i}", "tool": "t"}))
        entry.chmod(0o000)
    journal = CallJournal()
    try:
        assert not legacy.exists()
        assert not (_owner() / "call.legacy").exists()
        with pytest.raises(NoCallError):
            journal.resume("c1")
    finally:
        journal.close()


def test_retire_removes_an_answered_entry_and_leaves_an_unanswered_one() -> None:
    journal = CallJournal()
    try:
        assert journal.receive("open", "t", {})
        with pytest.raises(call_journal.UnansweredError):
            journal.retire("open")
        assert (_owner() / "entry" / "open.json").exists()
        journal.finish("open", CallResult("done", False))
        journal.retire("open")
        assert not (_owner() / "entry" / "open.json").exists()
        with pytest.raises(NoCallError):
            journal.retire("open")
    finally:
        journal.close()


def test_the_journal_lists_no_directory() -> None:
    source = Path(call_journal.__file__).read_text()
    listing = {"listdir", "scandir", "iterdir", "glob", "rglob", "walk"}
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
    }
    assert not called & listing, f"call_journal.py calls {sorted(called & listing)}"


class _Unreached:
    """The listener behind the layer, which a request the layer answers itself never reaches."""

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        raise AssertionError(f"the listener was reached by {scope['method']} {scope['path']}")


def _retire(client: TestClient, call_id: str) -> dict[str, Any]:
    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": call_journal.RETIRE_METHOD, "params": {"call_id": call_id}})
    assert response.status_code == 200
    return response.json()


def test_brain_retire_over_the_listener_retires_an_answered_call_and_refuses_an_unanswered_one() -> None:
    journal = CallJournal()
    try:
        client = TestClient(cast(Any, CallJournalLayer(_Unreached(), journal, "/mcp")))
        assert journal.receive("done", "t", {})
        journal.finish("done", CallResult("answer", False))
        assert journal.receive("open", "t", {})

        assert _retire(client, "done") == {"jsonrpc": "2.0", "id": 4, "result": {}}
        assert not (_owner() / "entry" / "done.json").exists()
        with pytest.raises(NoCallError):
            journal.resume("done")
        assert _retire(client, "done") == {"jsonrpc": "2.0", "id": 4, "result": {}}

        refused = _retire(client, "open")
        assert refused["error"]["code"] == -32000
        assert (_owner() / "entry" / "open.json").exists()
    finally:
        journal.close()
