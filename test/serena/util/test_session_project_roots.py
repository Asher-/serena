"""Tests for :class:`ExplicitProjectRootStore`.

The store exists so that the record of a session's explicit project activation
outlives the daemon process. Every test here is therefore written against a
*fresh store instance* over the same path wherever the cross-restart property is
what matters -- reusing one instance would pass even if the data never reached
the disk.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from serena.config.serena_config import SerenaPaths
from serena.util.session_project_roots import ExplicitProjectRootStore


def _store(tmp_path: Path, **kwargs: float) -> ExplicitProjectRootStore:
    return ExplicitProjectRootStore(str(tmp_path / "state" / "session_project_roots.json"), **kwargs)


class TestConftestIsolationGuardIsInEffect:
    """The blanket guard in ``test/serena/conftest.py`` has a silent failure mode.

    ``_isolate_explicit_project_root_store`` redirects the store by patching the
    :class:`SerenaPaths` *singleton*, and :class:`SerenaAgent` binds its own store
    from ``SerenaPaths().explicit_project_roots_file`` at construction. Drop the
    ``@singleton`` decorator and the patch lands on a throwaway instance, so the
    agent silently resumes writing into the developer's real ``~/.serena`` -- the
    exact defect the fixture exists to prevent -- with nothing turning red.

    Every other test in this package binds its own store explicitly, so none of
    them exercises the fixture: delete it and they all still pass. That is why
    these two tests are here, and why they deliberately assert on the *resolved
    path* rather than on the real file's contents. The fixture's own docstring
    declines the latter as flaky, and it is right to -- a serena daemon running
    on the same machine writes that file legitimately.

    Not covered, and stated rather than implied: if ``SerenaAgent`` ever stops
    reading its path from ``SerenaPaths()``, these assertions still pass while
    the agent pollutes. Catching that needs a constructed agent, which is far
    heavier than this module's scope.
    """

    def test_the_path_an_agent_would_bind_is_not_the_real_user_store(self) -> None:
        bound = Path(SerenaPaths().explicit_project_roots_file).resolve()
        real = (Path.home() / ".serena" / "session_project_roots.json").resolve()
        assert bound != real, (
            "the conftest fixture is not covering SerenaPaths -- a SerenaAgent built in this "
            "suite would write the developer's real activation store. Most likely cause: "
            "SerenaPaths lost its @singleton decorator, so the fixture patched a throwaway instance"
        )

    def test_a_store_built_the_way_the_agent_builds_it_writes_inside_the_sandbox(self) -> None:
        """Proves the redirect is live, not merely a different-looking string."""
        path = Path(SerenaPaths().explicit_project_roots_file)
        store = ExplicitProjectRootStore(str(path))
        key = "conftest-isolation-guard-probe"
        try:
            store.set(key, "/tmp/isolation-guard-probe-root")
            assert path.is_file(), "the redirected store path was not written through"
            assert ExplicitProjectRootStore(str(path)).get(key) == "/tmp/isolation-guard-probe-root"
            assert Path.home() / ".serena" not in path.parents, f"the suite wrote an activation record under the real user store: {path}"
        finally:
            store.discard(key)


class TestRoundTrip:
    def test_set_then_get_returns_the_root(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set("cc-session-a", "/tmp/project-a")
        assert store.get("cc-session-a") == "/tmp/project-a"

    def test_unknown_key_returns_none(self, tmp_path: Path) -> None:
        assert _store(tmp_path).get("never-seen") is None

    def test_a_fresh_instance_over_the_same_path_sees_the_record(self, tmp_path: Path) -> None:
        """The whole point: a new process must be able to read what the previous one wrote."""
        _store(tmp_path).set("cc-session-a", "/tmp/project-a")
        assert _store(tmp_path).get("cc-session-a") == "/tmp/project-a", (
            "the record must survive the store instance that wrote it, or it cannot survive a daemon restart"
        )

    def test_set_overwrites_a_previous_root_for_the_same_session(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set("cc-session-a", "/tmp/project-a")
        store.set("cc-session-a", "/tmp/project-b")
        assert _store(tmp_path).get("cc-session-a") == "/tmp/project-b"

    def test_sessions_do_not_collide(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set("cc-session-a", "/tmp/project-a")
        store.set("cc-session-b", "/tmp/project-b")
        reloaded = _store(tmp_path)
        assert reloaded.get("cc-session-a") == "/tmp/project-a"
        assert reloaded.get("cc-session-b") == "/tmp/project-b"

    def test_the_parent_directory_is_created_on_first_write(self, tmp_path: Path) -> None:
        path = tmp_path / "does" / "not" / "exist" / "roots.json"
        ExplicitProjectRootStore(str(path)).set("cc-session-a", "/tmp/project-a")
        assert path.is_file()


class TestNonDurableKeys:
    """Tier-3 session keys are ``id(mcp_ctx.session)`` ints whose meaning ends with
    the process. Persisting one would let a future session at a recycled address
    inherit an unrelated project root.
    """

    def test_int_key_is_not_written(self, tmp_path: Path) -> None:
        path = tmp_path / "state" / "session_project_roots.json"
        store = ExplicitProjectRootStore(str(path))
        store.set(140234981234, "/tmp/project-a")
        assert not path.exists(), "an id()-derived key must never reach the disk"

    def test_int_key_reads_as_absent(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set(140234981234, "/tmp/project-a")
        assert store.get(140234981234) is None

    def test_int_key_discard_is_a_noop(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set("cc-session-a", "/tmp/project-a")
        store.discard(140234981234)
        assert store.get("cc-session-a") == "/tmp/project-a"


class TestDiscard:
    def test_discard_removes_the_record(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set("cc-session-a", "/tmp/project-a")
        store.discard("cc-session-a")
        assert _store(tmp_path).get("cc-session-a") is None

    def test_discard_leaves_other_sessions_intact(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set("cc-session-a", "/tmp/project-a")
        store.set("cc-session-b", "/tmp/project-b")
        store.discard("cc-session-a")
        assert _store(tmp_path).get("cc-session-b") == "/tmp/project-b"

    def test_discarding_an_unknown_key_is_a_noop(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.discard("never-seen")
        assert store.get("never-seen") is None


class TestPruning:
    """Tier-2 entries are never evicted in-process, so the file needs an age bound
    or it grows for the life of the installation.
    """

    def test_an_entry_older_than_the_max_age_is_dropped(self, tmp_path: Path) -> None:
        path = tmp_path / "state" / "session_project_roots.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"cc-session-stale": {"root": "/tmp/project-a", "updated_at": time.time() - 100}}),
            encoding="utf-8",
        )
        assert ExplicitProjectRootStore(str(path), max_age_seconds=10).get("cc-session-stale") is None

    def test_a_fresh_entry_is_kept(self, tmp_path: Path) -> None:
        store = _store(tmp_path, max_age_seconds=3600)
        store.set("cc-session-a", "/tmp/project-a")
        assert store.get("cc-session-a") == "/tmp/project-a"

    def test_pruning_is_persisted_on_the_next_write(self, tmp_path: Path) -> None:
        path = tmp_path / "state" / "session_project_roots.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "cc-session-stale": {"root": "/tmp/project-a", "updated_at": time.time() - 100},
                    "cc-session-fresh": {"root": "/tmp/project-b", "updated_at": time.time()},
                }
            ),
            encoding="utf-8",
        )
        ExplicitProjectRootStore(str(path), max_age_seconds=10).set("cc-session-c", "/tmp/project-c")
        assert set(json.loads(path.read_text(encoding="utf-8"))) == {"cc-session-fresh", "cc-session-c"}


class TestDegradesRatherThanRaises:
    """A store that cannot be read or written costs the cross-restart restore. It
    must never break an activation or a tool call.
    """

    def test_unparseable_file_reads_as_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "state" / "session_project_roots.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json at all", encoding="utf-8")
        assert ExplicitProjectRootStore(str(path)).get("cc-session-a") is None

    def test_a_json_scalar_instead_of_an_object_reads_as_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "state" / "session_project_roots.json"
        path.parent.mkdir(parents=True)
        path.write_text('"a string"', encoding="utf-8")
        assert ExplicitProjectRootStore(str(path)).get("cc-session-a") is None

    def test_a_corrupt_file_can_still_be_written_over(self, tmp_path: Path) -> None:
        path = tmp_path / "state" / "session_project_roots.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json at all", encoding="utf-8")
        store = ExplicitProjectRootStore(str(path))
        store.set("cc-session-a", "/tmp/project-a")
        assert ExplicitProjectRootStore(str(path)).get("cc-session-a") == "/tmp/project-a"

    def test_malformed_entries_are_skipped_not_fatal(self, tmp_path: Path) -> None:
        path = tmp_path / "state" / "session_project_roots.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "no-root": {"updated_at": time.time()},
                    "root-not-a-string": {"root": 17, "updated_at": time.time()},
                    "no-timestamp": {"root": "/tmp/project-a"},
                    "entry-not-an-object": "/tmp/project-b",
                    "good": {"root": "/tmp/project-c", "updated_at": time.time()},
                }
            ),
            encoding="utf-8",
        )
        store = ExplicitProjectRootStore(str(path))
        assert store.get("good") == "/tmp/project-c"
        for bad_key in ("no-root", "root-not-a-string", "no-timestamp", "entry-not-an-object"):
            assert store.get(bad_key) is None

    def test_an_unwritable_path_does_not_raise(self, tmp_path: Path) -> None:
        """The store path sits under a regular file, so makedirs/open both fail."""
        blocker = tmp_path / "blocker"
        blocker.write_text("", encoding="utf-8")
        store = ExplicitProjectRootStore(str(blocker / "nested" / "roots.json"))
        store.set("cc-session-a", "/tmp/project-a")  # must not raise
        assert store.get("cc-session-a") is None

    def test_no_temporary_file_is_left_behind(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set("cc-session-a", "/tmp/project-a")
        assert list((tmp_path / "state").glob("*.tmp")) == []
