"""unit tests for :mod:`serena.util.file_lifecycle` -- the reflex filesystem lifecycle service.

The service is the object-oriented core behind the T5 lifecycle tools (spec-v2 §5.4): typed,
never-raising ``list_dir`` / ``find_file`` / ``stat`` / ``create`` / ``delete`` / ``rename`` over a
single project root (read-root == write-root), with the §5.5 optimistic-concurrency
``expect_version`` compare-and-swap forward-pulled onto the mutators.

These tests exercise the service directly against a temp directory -- no agent / tool harness --
pinning the acceptance criteria the plan attaches to T5: list-edges (typed entries, symlink
no-follow, hidden/gitignore default-skip with opt-out, truncation), find-policy (glob/substring
on paths, gitignore-filtered), stat (kind/size/eol/trailing-newline/permission/version), lifecycle
atomicity (nonclobber + crashsafe), and staleness-soundness (CAS mismatch returns a typed stale
state carrying the current bytes and never writes).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from serena.util.file_lifecycle import EntryKind, FilesystemLifecycle, content_version


def _fs(root: Path) -> FilesystemLifecycle:
    # the service binds to a single root; with no injected ignore predicate it falls back to a
    # GitignoreParser over that root, so a .gitignore dropped into tmp_path is honored.
    return FilesystemLifecycle(str(root))


# --- content_version: the §5.5 optimistic-concurrency token ---


class TestContentVersion:
    def test_is_sha256_prefix(self) -> None:
        data = b"hello world"
        assert content_version(data) == hashlib.sha256(data).hexdigest()[:16]

    def test_changes_when_bytes_change(self) -> None:
        assert content_version(b"a") != content_version(b"b")

    def test_stable_across_calls(self) -> None:
        assert content_version(b"same") == content_version(b"same")


# --- list_dir: typed entries, symlink no-follow, hidden/gitignore default-skip (spec-v2 §5.4) ---


class TestListDir:
    def test_lists_files_and_dirs_as_typed_entries(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("hi")
        (tmp_path / "sub").mkdir()

        listing = _fs(tmp_path).list_dir(".")

        by_name = {e.name: e for e in listing.entries}
        assert by_name["a.txt"].kind is EntryKind.FILE
        assert by_name["a.txt"].size == 2
        assert by_name["sub"].kind is EntryKind.DIR
        assert listing.exists is True
        assert listing.truncated is False
        assert listing.error is None

    def test_entry_carries_mtime(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("x")
        entry = {e.name: e for e in _fs(tmp_path).list_dir(".").entries}["a.txt"]
        assert entry.mtime > 0

    def test_hidden_skipped_by_default_and_opt_in(self, tmp_path: Path) -> None:
        (tmp_path / ".secret").write_text("x")
        (tmp_path / "visible.txt").write_text("y")

        default = _fs(tmp_path).list_dir(".")
        assert {e.name for e in default.entries} == {"visible.txt"}

        with_hidden = _fs(tmp_path).list_dir(".", include_hidden=True)
        assert ".secret" in {e.name for e in with_hidden.entries}

    def test_gitignored_skipped_by_default_and_opt_in(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("*.log\n")
        (tmp_path / "keep.txt").write_text("k")
        (tmp_path / "drop.log").write_text("d")

        default = _fs(tmp_path).list_dir(".")
        names = {e.name for e in default.entries}
        assert "keep.txt" in names
        assert "drop.log" not in names

        with_ignored = _fs(tmp_path).list_dir(".", include_ignored=True)
        assert "drop.log" in {e.name for e in with_ignored.entries}

    def test_symlink_reported_and_not_followed(self, tmp_path: Path) -> None:
        (tmp_path / "real.txt").write_text("hello")
        os.symlink(tmp_path / "real.txt", tmp_path / "link.txt")

        entry = {e.name: e for e in _fs(tmp_path).list_dir(".").entries}["link.txt"]
        assert entry.kind is EntryKind.SYMLINK
        assert entry.symlink_target == str(tmp_path / "real.txt")

    def test_recursive_lists_nested_but_does_not_traverse_symlinked_dir(self, tmp_path: Path) -> None:
        (tmp_path / "realdir").mkdir()
        (tmp_path / "realdir" / "inside.txt").write_text("x")
        os.symlink(tmp_path / "realdir", tmp_path / "linkdir")

        names = {e.name for e in _fs(tmp_path).list_dir(".", recursive=True).entries}
        # the real nested file is reached, the symlinked directory is reported but not descended into
        assert "realdir/inside.txt" in names
        assert "linkdir" in names
        assert "linkdir/inside.txt" not in names

    def test_max_entries_truncates_with_footer(self, tmp_path: Path) -> None:
        for i in range(5):
            (tmp_path / f"f{i}.txt").write_text("x")

        listing = _fs(tmp_path).list_dir(".", max_entries=3)
        assert len(listing.entries) == 3
        assert listing.truncated is True

    def test_not_found_is_typed_and_never_raises(self, tmp_path: Path) -> None:
        listing = _fs(tmp_path).list_dir("does/not/exist")
        assert listing.exists is False
        assert listing.error is not None
        assert listing.entries == ()

    def test_listing_a_regular_file_is_a_typed_error(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("x")
        listing = _fs(tmp_path).list_dir("f.txt")
        assert listing.error is not None


# --- find_file: glob/substring on paths, gitignore-filtered (spec-v2 §5.4) ---


class TestFindFile:
    def test_glob_matches_paths_anywhere(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "b.txt").write_text("y")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "c.py").write_text("z")

        result = _fs(tmp_path).find_file("*.py")
        assert set(result.matches) == {"a.py", "sub/c.py"}

    def test_substring_matches_paths(self, tmp_path: Path) -> None:
        (tmp_path / "alpha.txt").write_text("x")
        (tmp_path / "beta.txt").write_text("y")

        result = _fs(tmp_path).find_file("alph")
        assert result.matches == ("alpha.txt",)

    def test_matches_on_path_not_on_contents(self, tmp_path: Path) -> None:
        # a file whose CONTENTS contain the term but whose PATH does not must NOT match
        # (find_file is path-only; content search is search_for_pattern's job)
        (tmp_path / "data.txt").write_text("needle in the content")
        result = _fs(tmp_path).find_file("needle")
        assert result.matches == ()

    def test_gitignored_files_excluded(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("*.log\n")
        (tmp_path / "keep.py").write_text("x")
        (tmp_path / "skip.log").write_text("y")

        result = _fs(tmp_path).find_file("*")
        assert "skip.log" not in result.matches
        assert "keep.py" in result.matches

    def test_max_results_truncates(self, tmp_path: Path) -> None:
        for i in range(5):
            (tmp_path / f"f{i}.py").write_text("x")

        result = _fs(tmp_path).find_file("*.py", max_results=2)
        assert len(result.matches) == 2
        assert result.truncated is True

    def test_relative_path_scopes_the_search(self, tmp_path: Path) -> None:
        (tmp_path / "top.py").write_text("x")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "nested.py").write_text("y")

        result = _fs(tmp_path).find_file("*.py", relative_path="sub")
        assert result.matches == ("sub/nested.py",)

    def test_render_never_names_search_for_pattern(self, tmp_path: Path) -> None:
        # the block-hook the mandate targets forbids routing agents to the deleted search tool
        (tmp_path / "a.py").write_text("x")
        assert "search_for_pattern" not in _fs(tmp_path).find_file("*.py").render()


# --- stat: kind/size/eol/trailing-newline/permission/version (spec-v2 §5.4/§5.9) ---


class TestStat:
    def test_file_metadata(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_bytes(b"line1\nline2\n")

        st = _fs(tmp_path).stat("f.txt")
        assert st.exists is True
        assert st.kind is EntryKind.FILE
        assert st.size == 12
        assert st.eol == "LF"
        assert st.trailing_newline is True
        assert st.encoding == "utf-8"
        assert st.is_binary is False
        assert st.version == content_version(b"line1\nline2\n")
        assert st.permissions  # a non-empty rendering of the mode bits

    def test_no_trailing_newline_reported(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_bytes(b"no newline here")
        st = _fs(tmp_path).stat("f.txt")
        assert st.trailing_newline is False
        assert st.eol == "none"

    def test_crlf_reported(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_bytes(b"a\r\nb\r\n")
        assert _fs(tmp_path).stat("f.txt").eol == "CRLF"

    def test_binary_reported(self, tmp_path: Path) -> None:
        (tmp_path / "b.bin").write_bytes(b"\x00\x01\x02\x03")
        assert _fs(tmp_path).stat("b.bin").is_binary is True

    def test_directory_stat(self, tmp_path: Path) -> None:
        (tmp_path / "d").mkdir()
        st = _fs(tmp_path).stat("d")
        assert st.exists is True
        assert st.kind is EntryKind.DIR

    def test_symlink_stat_does_not_follow(self, tmp_path: Path) -> None:
        (tmp_path / "real.txt").write_text("x")
        os.symlink(tmp_path / "real.txt", tmp_path / "link.txt")

        st = _fs(tmp_path).stat("link.txt")
        assert st.kind is EntryKind.SYMLINK
        assert st.symlink_target == str(tmp_path / "real.txt")

    def test_not_found_is_typed(self, tmp_path: Path) -> None:
        st = _fs(tmp_path).stat("nope.txt")
        assert st.exists is False
        assert st.error is not None

    def test_version_is_the_token_the_mutators_accept(self, tmp_path: Path) -> None:
        # stat's version is exactly the expect_version a mutator compares against
        (tmp_path / "f.txt").write_text("hello")
        st = _fs(tmp_path).stat("f.txt")
        result = _fs(tmp_path).delete("f.txt", expect_version=st.version)
        assert result.ok is True


# --- create: atomic (temp+os.replace) + create-vs-overwrite intent + CAS (spec-v2 §5.4/§5.5) ---


class TestCreate:
    def test_create_new_file(self, tmp_path: Path) -> None:
        result = _fs(tmp_path).create("new.txt", "hello")
        assert result.ok is True
        assert (tmp_path / "new.txt").read_text() == "hello"

    def test_create_makes_parent_directories(self, tmp_path: Path) -> None:
        result = _fs(tmp_path).create("a/b/c.txt", "x")
        assert result.ok is True
        assert (tmp_path / "a" / "b" / "c.txt").read_text() == "x"

    def test_create_without_overwrite_refuses_existing_and_leaves_original(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("ORIGINAL")
        result = _fs(tmp_path).create("f.txt", "NEW")
        assert result.ok is False
        assert (tmp_path / "f.txt").read_text() == "ORIGINAL"

    def test_overwrite_unconditional(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("OLD")
        result = _fs(tmp_path).create("f.txt", "NEW", overwrite=True)
        assert result.ok is True
        assert (tmp_path / "f.txt").read_text() == "NEW"

    def test_overwrite_cas_match_writes(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("OLD")
        result = _fs(tmp_path).create("f.txt", "NEW", overwrite=True, expect_version=content_version(b"OLD"))
        assert result.ok is True
        assert (tmp_path / "f.txt").read_text() == "NEW"

    def test_overwrite_cas_mismatch_returns_stale_with_current_bytes_and_does_not_write(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("OLD")
        result = _fs(tmp_path).create("f.txt", "NEW", overwrite=True, expect_version="0" * 16)
        assert result.ok is False
        assert result.stale is not None
        assert result.stale.expected == "0" * 16
        assert result.stale.actual == content_version(b"OLD")
        assert result.stale.current_bytes == b"OLD"
        # the original bytes are untouched: a stale CAS never writes
        assert (tmp_path / "f.txt").read_text() == "OLD"

    def test_create_is_crashsafe_original_survives_a_failed_replace(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "f.txt").write_text("ORIGINAL")

        def boom(src: str, dst: str) -> None:
            raise OSError("simulated crash during os.replace")

        monkeypatch.setattr(os, "replace", boom)

        result = _fs(tmp_path).create("f.txt", "NEW", overwrite=True)
        assert result.ok is False
        # temp+replace means the target is only ever swapped atomically: a failed replace leaves the
        # original intact (a truncate-then-write would already have destroyed it before failing)
        assert (tmp_path / "f.txt").read_text() == "ORIGINAL"
        # and the temp file is cleaned up rather than left as litter next to the target
        assert {p.name for p in tmp_path.iterdir()} == {"f.txt"}

    def test_overwrite_preserves_executable_mode(self, tmp_path: Path) -> None:
        # mkstemp creates the temp at 0o600, so a naive temp+os.replace overwrite would strip the
        # target's +x bits; _atomic_write captures the prior mode and restores it on an overwrite.
        target = tmp_path / "run.sh"
        target.write_text("#!/bin/sh\necho old\n")
        os.chmod(target, 0o755)

        result = _fs(tmp_path).create("run.sh", "#!/bin/sh\necho new\n", overwrite=True)

        assert result.ok is True
        assert target.read_text() == "#!/bin/sh\necho new\n"
        # the executable bits survive the atomic overwrite (mode preserved, not reset to 0o600)
        assert os.stat(target).st_mode & 0o111 == 0o111


# --- delete: idempotent + CAS (spec-v2 §5.4/§5.5) ---


class TestDelete:
    def test_delete_existing(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("x")
        result = _fs(tmp_path).delete("f.txt")
        assert result.ok is True
        assert not (tmp_path / "f.txt").exists()

    def test_delete_is_idempotent_on_missing(self, tmp_path: Path) -> None:
        result = _fs(tmp_path).delete("ghost.txt")
        assert result.ok is True

    def test_delete_cas_match(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("x")
        result = _fs(tmp_path).delete("f.txt", expect_version=content_version(b"x"))
        assert result.ok is True
        assert not (tmp_path / "f.txt").exists()

    def test_delete_cas_mismatch_is_stale_and_leaves_file(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("x")
        result = _fs(tmp_path).delete("f.txt", expect_version="f" * 16)
        assert result.ok is False
        assert result.stale is not None
        assert (tmp_path / "f.txt").exists()


# --- rename == move: one verb, CAS on source, containment (spec-v2 §5.4/§5.5) ---


class TestRename:
    def test_rename_within_directory(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("x")
        result = _fs(tmp_path).rename("a.txt", "b.txt")
        assert result.ok is True
        assert not (tmp_path / "a.txt").exists()
        assert (tmp_path / "b.txt").read_text() == "x"

    def test_move_to_subdirectory_creates_parents(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("x")
        result = _fs(tmp_path).rename("a.txt", "sub/dir/a.txt")
        assert result.ok is True
        assert (tmp_path / "sub" / "dir" / "a.txt").read_text() == "x"

    def test_rename_cas_match(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("x")
        result = _fs(tmp_path).rename("a.txt", "b.txt", expect_version=content_version(b"x"))
        assert result.ok is True

    def test_rename_cas_mismatch_is_stale_and_leaves_source(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("x")
        result = _fs(tmp_path).rename("a.txt", "b.txt", expect_version="0" * 16)
        assert result.ok is False
        assert result.stale is not None
        assert (tmp_path / "a.txt").exists()
        assert not (tmp_path / "b.txt").exists()

    def test_rename_missing_source_is_typed(self, tmp_path: Path) -> None:
        result = _fs(tmp_path).rename("ghost.txt", "b.txt")
        assert result.ok is False


# --- containment: read-root == write-root; nothing escapes the root (spec-v2 §5.4) ---


class TestContainment:
    def test_list_dir_rejects_escape(self, tmp_path: Path) -> None:
        listing = _fs(tmp_path).list_dir("../..")
        assert listing.error is not None

    def test_create_rejects_escape(self, tmp_path: Path) -> None:
        result = _fs(tmp_path).create("../evil.txt", "x")
        assert result.ok is False
        assert not (tmp_path.parent / "evil.txt").exists()

    def test_rename_rejects_escaping_destination(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("x")
        result = _fs(tmp_path).rename("a.txt", "../evil.txt")
        assert result.ok is False
        assert (tmp_path / "a.txt").exists()
