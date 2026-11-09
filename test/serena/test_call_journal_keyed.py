"""
The call journal keeps each entry at <owner>/entry/<id>.json and reads it only by its id, removes the journal of the
layout before it whole by its known path, and retires an answered entry (task://retention_keyed_stores.8).
"""

import ast
import json
from pathlib import Path

import pytest

from serena import call_journal
from serena.call_journal import CallJournal, CallResult, NoCallError
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
