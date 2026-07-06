"""THE certification suite for serena's total-file-access mandate (spec-v2 §5.10).

This module is the CI anti-regression gate. It consolidates the mandate-level R1 + R2
invariants -- the ones that together certify serena as a 100% total file-access
replacement -- over a single canonical fixture covering every file kind. Its green
state is the guarantee that a fourth *wording-only* attempt (adjust a tool message or a
hook redirect without adding capability) is structurally impossible: the invariants here
are byte-level and behavioral, so wording can never satisfy them.

SCOPE DISCIPLINE (spec-v2 §5.10: "scoped to invariants, must NOT metastasize"). This
suite pins the INVARIANTS, not per-branch coverage. Exhaustive per-capability tests live
in the task modules and are NOT duplicated here:

* read floor / plaintext ....... test_cursor_plaintext.py, test_plaintext_floor.py
* line-number contract ......... test_cursor_line_base.py, util/test_line_numbers.py
* lifecycle plane .............. test_file_lifecycle.py, test_file_tools.py
* tree-sitter rung ............. test_cursor_treesitter.py, structural/backends/test_treesitter.py
* staleness CAS ................ test_cursor_staleness.py, util/test_staleness.py
* huge-file windowing .......... test_cursor_windowing.py, util/test_windowing.py

R1 (read floor, over the fixture): zero-unreadable-bytes round-trip through the cursor
surface; structural round-trip; 1-based cat -n line numbers with no 0-based leak; no
read path dead-ends or routes to a deleted tool. R2 (gap closures): lifecycle plane,
cursor-fate, optimistic-concurrency staleness, huge-file windowing, tree-sitter rung.

The bulk is hermetic (a MagicMock-project ``CursorManager`` + the plaintext floor and
structural registry directly), so the gate is fast and deterministic. The read<->write
symmetry and out-of-band staleness invariants inherently need a live language server and
are marked ``@pytest.mark.python``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from serena.cursor import CursorManager, ReadRung
from serena.tools.cursor_tools import CursorReplaceRangeTool
from serena.util.file_lifecycle import EntryKind, FilesystemLifecycle, content_version
from serena.util.staleness import read_file_version
from solidlsp.structural.registry import default_structural_backend_registry

# --------------------------------------------------------------------------- #
# Harness (mirrors test_cursor_plaintext / test_cursor_windowing verbatim so the
# cert exercises the same surface the per-capability suites do).
# --------------------------------------------------------------------------- #


def _manager(tmp_path: Path) -> CursorManager:
    """A CursorManager over ``tmp_path`` with a stub project (default 13-backend registry)."""
    project = MagicMock()
    project.project_root = str(tmp_path)
    project.read_file = MagicMock(side_effect=lambda p: Path(tmp_path / p).read_text(encoding="utf-8"))
    return CursorManager(project)


def _force_lsp_miss():
    """Patch the LSP retriever so no file analyzes and no symbol resolves -> the ladder
    falls through to the structural (tree-sitter/explicit) rung or the plaintext floor.
    """
    retriever = MagicMock()
    retriever.find_unique.side_effect = ValueError("no symbol")
    retriever.can_analyze_file.return_value = False
    return patch.object(CursorManager, "_retriever", new_callable=PropertyMock, return_value=retriever)


def _body_content(view: str) -> list[str]:
    """Extract the numbered body content lines (the ``N: text`` payload), dropping metadata."""
    out: list[str] = []
    in_body = False
    for ln in view.splitlines():
        if ln == "--- body ---":
            in_body = True
            continue
        if ln == "--- end body ---":
            in_body = False
            continue
        if in_body and ln.startswith(("window:", "continuation:")):
            continue
        if in_body:
            _num, _sep, content = ln.partition(": ")
            out.append(content)
    return out


def _continuation(view: str) -> str | None:
    for ln in view.splitlines():
        if ln.startswith("continuation: "):
            return ln[len("continuation: ") :]
    return None


def _projected_version(view: str) -> str:
    for line in view.splitlines():
        if line.startswith("version:"):
            return line.split("version:", 1)[1].strip()
    raise AssertionError(f"no 'version:' line in projection:\n{view}")


def _anchor_line(view: str) -> str:
    return next(line for line in view.splitlines() if line.startswith("@ "))


# --------------------------------------------------------------------------- #
# The canonical fixture: every file kind the mandate must serve (spec-v2 §5.10).
# --------------------------------------------------------------------------- #

_FIXTURE: dict[str, bytes] = {
    # LSP / explicit-structural languages
    "mod.py": b'import os\n\n\ndef greet(name):\n    return f"hi {name}"\n',
    "app.ts": b"export const answer: number = 42;\n",
    "readme.md": b"# Title\n\nA paragraph of prose.\n",
    "conf.yaml": b"services:\n  web:\n    image: nginx:latest\n",
    "conf.yml": b"key: value\n",
    "data.json": b'{\n  "a": 1,\n  "b": [2, 3]\n}\n',
    "pyproject.toml": b"[tool.ruff]\nline-length = 140\n",
    # tree-sitter fallback rung (grammar, no explicit backend)
    "styles.css": b"a {\n  color: red;\n}\n",
    "run.sh": b"#!/bin/sh\necho hi\n",
    "Dockerfile": b"FROM alpine:3\nRUN echo hi\n",
    "setup.ini": b"[section]\nkey = val\n",
    "data.xml": b"<root>\n  <item>x</item>\n</root>\n",
    "page.html": b"<html>\n  <body>hi</body>\n</html>\n",
    # plaintext floor (no grammar / suffixless)
    "notes.txt": b"line one\nline two\nline three\n",
    ".env": b"TOKEN=abc123\nDEBUG=true\n",
    "LICENSE": b"MIT License\n\nPermission is hereby granted.\n",
    ".gitignore": b"*.log\n__pycache__/\n",
    # edge-case byte shapes (spec-v2 §5.9)
    "blob.bin": bytes([0x00, 0x01, 0x02, 0xFF, 0xFE, 0x00, 0x0A]),
    "empty.txt": b"",
    "crlf.txt": b"a\r\nb\r\nc\r\n",
    "noeol.txt": b"no trailing newline",
    "latin1.txt": b"caf\xe9 na\xefve\n",  # invalid UTF-8 -> declared-fallback decode
}

# structural-rung kinds: resolve to a structural backend and round-trip byte-exact
_STRUCTURAL_KINDS = [
    "mod.py",
    "app.ts",
    "readme.md",
    "conf.yaml",
    "conf.yml",
    "data.json",
    "pyproject.toml",
    "styles.css",
    "run.sh",
    "Dockerfile",
    "setup.ini",
    "data.xml",
    "page.html",
]
# plaintext-text kinds: land a whole-file plaintext cursor whose numbered body reconstructs the lines
_PLAINTEXT_TEXT_KINDS = ["notes.txt", ".env", "LICENSE", ".gitignore", "crlf.txt", "noeol.txt"]

_ALL_KINDS = list(_FIXTURE)


@pytest.fixture
def fixture_dir(tmp_path: Path) -> Path:
    """Materialize the canonical fixture (plus a symlink) under a temp root."""
    for name, data in _FIXTURE.items():
        (tmp_path / name).write_bytes(data)
    os.symlink(tmp_path / "notes.txt", tmp_path / "link.txt")  # the symlink kind (§5.10)
    return tmp_path


# ===========================================================================
# R1 -- read floor: nothing unreadable, positions honest, no dead-end
# ===========================================================================


class TestR1ZeroUnreadableBytes:
    """The floor reconstructs the exact on-disk bytes of EVERY file kind -- the
    mandate's core promise (invariant 1: zero content unreadable through the surface).
    """

    @pytest.mark.parametrize("name", _ALL_KINDS)
    def test_floor_reconstructs_bytes(self, fixture_dir: Path, name: str) -> None:
        view = _manager(fixture_dir)._plaintext_view(name)  # the surface's raw-bytes boundary
        assert view.raw_bytes == _FIXTURE[name], f"floor did not reconstruct {name} byte-for-byte"

    def test_binary_is_a_typed_state_not_a_raise(self, fixture_dir: Path) -> None:
        view = _manager(fixture_dir)._plaintext_view("blob.bin")
        assert view.is_binary is True
        assert view.raw_bytes == _FIXTURE["blob.bin"]  # bytes still held, never lost

    def test_non_utf8_is_declared_fallback_not_a_raise(self, fixture_dir: Path) -> None:
        view = _manager(fixture_dir)._plaintext_view("latin1.txt")
        assert view.raw_bytes == _FIXTURE["latin1.txt"]
        assert view.is_binary is False  # decodable text, just not strict UTF-8


class TestR1NoDeadEnd:
    """No file kind dead-ends: every one resolves to a rung that lands on content, and
    no read projection raises or routes the agent to a deleted/blocked tool
    (invariant 2: no agent ever believes it cannot access something).
    """

    @pytest.mark.parametrize("name", _ALL_KINDS)
    def test_every_kind_resolves_to_a_landing_rung(self, fixture_dir: Path, name: str) -> None:
        manager = _manager(fixture_dir)
        with _force_lsp_miss():
            rung = manager.resolve_read_rung(name)  # must not raise
        assert rung in (ReadRung.STRUCTURAL, ReadRung.PLAINTEXT)

    @pytest.mark.parametrize("name", _ALL_KINDS)
    def test_no_overview_dead_ends_or_names_a_deleted_tool(self, fixture_dir: Path, name: str) -> None:
        manager = _manager(fixture_dir)
        with _force_lsp_miss():
            rung = manager.resolve_read_rung(name)
            out = manager.structural_overview(name) if rung is ReadRung.STRUCTURAL else manager.plaintext_overview(name)
        text = str(out)
        assert "Cannot extract symbols" not in text  # the killed dead-end message
        assert "search_for_pattern" not in text  # never routes to the deleted search tool


class TestR1StructuralRoundTrip:
    """structural-symmetry: for every structurally-analyzed kind, ``serialize(parse(x)) == x``
    -- the byte-exact read guarantee the round-trip cert rests on for rich rungs.
    """

    @pytest.mark.parametrize("name", _STRUCTURAL_KINDS)
    def test_serialize_of_parse_is_identity(self, fixture_dir: Path, name: str) -> None:
        registry = default_structural_backend_registry()
        backend = registry.structural_backend_for(name)
        assert backend is not None, f"{name} should resolve to a structural backend"
        src = _FIXTURE[name].decode("utf-8")
        assert backend.serialize(backend.parse(src)) == src


class TestR1PlaintextRoundTrip:
    """The whole-file plaintext cursor's numbered body reconstructs the file's lines
    through the cursor surface (round-trip through the floor rung).
    """

    @pytest.mark.parametrize("name", _PLAINTEXT_TEXT_KINDS)
    def test_body_reconstructs_lines(self, fixture_dir: Path, name: str) -> None:
        manager = _manager(fixture_dir)
        with _force_lsp_miss():
            cid, _ = manager.start_cursor(name, relative_path=name)
            view = manager.format_cursor_view(cid)
        expected = _FIXTURE[name].decode("utf-8").splitlines()
        assert _body_content(view) == expected


class TestR1LineNumberContract:
    """cat -n equivalence + no-0-based-leak: displayed line numbers are 1-based and no
    0-based integer ever crosses the agent boundary (spec-v2 §5.7).
    """

    def test_plaintext_body_is_one_based_no_zero_leak(self, fixture_dir: Path) -> None:
        manager = _manager(fixture_dir)
        with _force_lsp_miss():
            cid, _ = manager.start_cursor("notes.txt", relative_path="notes.txt")
            view = manager.format_cursor_view(cid)
        assert "1: line one" in view  # first line is 1, not 0 (cat -n)
        assert "2: line two" in view
        for ln in view.splitlines():  # no numbered body line may start at 0
            assert not ln.startswith("0: ")

    def test_structural_anchor_range_is_one_based_no_zero(self, fixture_dir: Path) -> None:
        manager = _manager(fixture_dir)
        with _force_lsp_miss():
            top = manager.structural_overview("styles.css")
            cid, _ = manager.start_cursor(top[0][0], relative_path="styles.css")
            view = manager.format_cursor_view(cid)
        anchor = _anchor_line(view)
        assert re.search(r"styles\.css:1(-\d+)?:", anchor), anchor  # rule starts at file line 1
        assert ":0:" not in view and ":0-" not in view  # the internal 0-based row never leaks


# ===========================================================================
# R2 -- gap closures: lifecycle, cursor-fate, staleness, windowing, tree-sitter
# ===========================================================================


class TestR2LifecyclePlane:
    """lifecycle-cover + list-edges + find-policy + atomicity: the restored filesystem
    plane (spec-v2 §5.4) is present, typed, containment-safe, and atomic.
    """

    def test_lifecycle_docs_the_six_tools_exist_and_mirror_upstream(self) -> None:
        # kills the jetbrains_tools.py:327 deleted-tool orphan: the signposted tools are real.
        from serena.tools.file_tools import (
            CreateTextFileTool,
            DeleteFileTool,
            FindFileTool,
            ListDirTool,
            RenameFileTool,
            StatTool,
        )

        names = {t.get_name_from_cls() for t in (ListDirTool, FindFileTool, StatTool, CreateTextFileTool, DeleteFileTool, RenameFileTool)}
        assert names == {"list_dir", "find_file", "stat", "create_text_file", "delete_file", "rename_file"}

    def test_cover_list_find_stat_create_delete_rename(self, tmp_path: Path) -> None:
        fs = FilesystemLifecycle(str(tmp_path))
        assert fs.create("a.txt", "hi").ok is True
        assert "a.txt" in {e.name for e in fs.list_dir(".").entries}
        assert fs.find_file("a.txt").matches == ("a.txt",)
        assert fs.stat("a.txt").kind is EntryKind.FILE
        assert fs.rename("a.txt", "b.txt").ok is True
        assert fs.delete("b.txt").ok is True

    def test_list_edges_hidden_gitignore_symlink(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("*.log\n")
        (tmp_path / ".secret").write_text("s")
        (tmp_path / "keep.txt").write_text("k")
        (tmp_path / "drop.log").write_text("d")
        os.symlink(tmp_path / "keep.txt", tmp_path / "link.txt")
        names = {e.name for e in FilesystemLifecycle(str(tmp_path)).list_dir(".").entries}
        assert ".secret" not in names and "drop.log" not in names  # hidden + gitignored skipped
        assert "keep.txt" in names
        link = {e.name: e for e in FilesystemLifecycle(str(tmp_path)).list_dir(".").entries}["link.txt"]
        assert link.kind is EntryKind.SYMLINK and link.symlink_target == str(tmp_path / "keep.txt")

    def test_find_policy_paths_only_and_never_names_search_for_pattern(self, tmp_path: Path) -> None:
        (tmp_path / "data.txt").write_text("needle in content")
        fs = FilesystemLifecycle(str(tmp_path))
        assert fs.find_file("needle").matches == ()  # path-only, not content
        assert "search_for_pattern" not in fs.find_file("*.txt").render()

    def test_atomicity_nonclobber_and_crashsafe(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fs = FilesystemLifecycle(str(tmp_path))
        (tmp_path / "f.txt").write_text("ORIGINAL")
        assert fs.create("f.txt", "NEW").ok is False  # nonclobber without intent
        assert (tmp_path / "f.txt").read_text() == "ORIGINAL"

        def boom(src: str, dst: str) -> None:
            raise OSError("crash during replace")

        monkeypatch.setattr(os, "replace", boom)
        assert fs.create("f.txt", "NEW", overwrite=True).ok is False  # crashsafe
        assert (tmp_path / "f.txt").read_text() == "ORIGINAL"  # original survives a failed replace

    def test_containment_read_root_equals_write_root(self, tmp_path: Path) -> None:
        fs = FilesystemLifecycle(str(tmp_path))
        assert fs.create("../evil.txt", "x").ok is False
        assert not (tmp_path.parent / "evil.txt").exists()


class TestR2StalenessCAS:
    """staleness-base: one byte-based token across surfaces (read projection == stat ==
    the gate) and the lifecycle mutators refuse a stale compare-and-swap without writing.
    """

    def test_staleness_base_one_token_across_surfaces(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_bytes(b"alpha\nbeta\n")
        manager = _manager(tmp_path)
        with _force_lsp_miss():
            cid, _ = manager.start_cursor("notes.txt", relative_path="notes.txt")
        projected = _projected_version(manager.format_cursor_view(cid))
        assert projected == FilesystemLifecycle(str(tmp_path)).stat("notes.txt").version
        assert projected == read_file_version(str(tmp_path), "notes.txt")

    def test_stale_cas_refuses_and_returns_current_bytes(self, tmp_path: Path) -> None:
        fs = FilesystemLifecycle(str(tmp_path))
        (tmp_path / "f.txt").write_text("OLD")
        result = fs.delete("f.txt", expect_version="0" * 16)
        assert result.ok is False
        assert result.stale is not None and result.stale.actual == content_version(b"OLD")
        assert (tmp_path / "f.txt").exists()  # a stale CAS never mutates


class TestR2CursorFate:
    """cursor-fate: a cursor whose file is deleted or renamed away renders a typed gone
    state -- never stale bytes, never a raise (spec-v2 §5.5).
    """

    def test_deleted_and_renamed_render_gone(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_bytes(b"alpha\nbeta\n")
        manager = _manager(tmp_path)
        with _force_lsp_miss():
            cid, _ = manager.start_cursor("notes.txt", relative_path="notes.txt")
        (tmp_path / "notes.txt").unlink()
        view = manager.format_cursor_view(cid)
        assert "no longer exists" in view and "notes.txt" in view


class TestR2HugeFileWindowing:
    """announce + hw-cover + boundary: every body carries a descriptor even untruncated,
    paging reconstructs the file across seams, and a CRLF pair / multibyte codepoint is
    never split (spec-v2 §5.6).
    """

    def test_announce_descriptor_always_present(self, tmp_path: Path) -> None:
        (tmp_path / "n.txt").write_bytes(b"a\nb\nc\n")
        manager = _manager(tmp_path)
        with _force_lsp_miss():
            cid, _ = manager.start_cursor("n.txt", relative_path="n.txt")
            view = manager.format_cursor_view(cid)
        assert "window: lines 1-3 of 3" in view
        assert "truncated=false" in view

    def test_hw_cover_paging_reconstructs_across_seams(self, tmp_path: Path) -> None:
        lines = [f"row{i}\n" for i in range(20)]
        (tmp_path / "big.txt").write_bytes("".join(lines).encode("utf-8"))
        manager = _manager(tmp_path)
        with _force_lsp_miss():
            cid, _ = manager.start_cursor("big.txt", relative_path="big.txt")
            collected: list[str] = []
            view = manager.format_cursor_view(cid, max_lines=5)
            pages = 0
            while True:
                collected.extend(_body_content(view))
                pages += 1
                token = _continuation(view)
                if token is None:
                    break
                assert pages < 100
                view = manager.format_cursor_view(cid, continuation=token, max_lines=5)
        assert collected == [ln.rstrip("\n") for ln in lines]
        assert pages == 4

    def test_boundary_crlf_and_multibyte_never_split(self, tmp_path: Path) -> None:
        (tmp_path / "c.txt").write_bytes(b"a\r\nb\r\nc\r\nd\r\n")
        manager = _manager(tmp_path)
        with _force_lsp_miss():
            cid, _ = manager.start_cursor("c.txt", relative_path="c.txt")
            p1 = manager.format_cursor_view(cid, max_lines=2)
            p2 = manager.format_cursor_view(cid, continuation=_continuation(p1), max_lines=2)
        assert _body_content(p1) == ["a", "b"] and _body_content(p2) == ["c", "d"]
        (tmp_path / "m.txt").write_bytes("café\nnaïve\n".encode())
        manager2 = _manager(tmp_path)
        with _force_lsp_miss():
            cid2, _ = manager2.start_cursor("m.txt", relative_path="m.txt")
            mv = manager2.format_cursor_view(cid2, max_lines=1)
        assert _body_content(mv) == ["café"] and "�" not in mv


class TestR2TreeSitterRung:
    """ts-0-leak + ERROR-fallthrough + structural-symmetry: the tree-sitter fallback rung
    reads grammarless-but-parseable files byte-exact, degrades on malformed source without
    raising, and never leaks a 0-based line (pins beb618e8) (spec-v2 §5.8).
    """

    def test_zero_leak_pinned(self, tmp_path: Path) -> None:
        (tmp_path / "styles.css").write_bytes(b"a {\n  color: red;\n}\n")
        manager = _manager(tmp_path)
        with _force_lsp_miss():
            top = manager.structural_overview("styles.css")
            cid, _ = manager.start_cursor(top[0][0], relative_path="styles.css")
            view = manager.format_cursor_view(cid)
        assert re.search(r"styles\.css:1(-\d+)?:", _anchor_line(view))
        assert ":0:" not in view and ":0-" not in view

    def test_error_fallthrough_never_raises(self, tmp_path: Path) -> None:
        (tmp_path / "bad.css").write_bytes(b"a {\n  color: red;")  # unterminated
        manager = _manager(tmp_path)
        with _force_lsp_miss():
            assert manager.resolve_read_rung("bad.css") is ReadRung.STRUCTURAL
            top = manager.structural_overview("bad.css")  # must not raise
        assert isinstance(top, list)

    def test_structural_symmetry_body_is_exact_slice(self, tmp_path: Path) -> None:
        (tmp_path / "styles.css").write_bytes(b"a {\n  color: red;\n}\n")
        manager = _manager(tmp_path)
        with _force_lsp_miss():
            top = manager.structural_overview("styles.css")
            cid, state = manager.start_cursor(top[0][0], relative_path="styles.css")
            state.include_body = True
            view = manager.format_cursor_view(cid)
        assert "a {\n  color: red;\n}" in view


# ===========================================================================
# R1 symmetry + R2 out-of-band staleness -- inherently need a live LSP
# ===========================================================================


@pytest.mark.python
class TestReadWriteSymmetryLive:
    """symmetry + staleness-soundness: a 1-based line a read surface displays is accepted
    verbatim by the write tool and lands on the same bytes; an out-of-band change refuses
    the write (spec-v2 §5.7 read<->write symmetry + §5.5 CAS soundness).
    """

    @pytest.fixture
    def sandbox(self, python_serena_agent):
        rel = os.path.join("test_repo", "_cert_symmetry_sandbox.py")
        abs_path = Path(python_serena_agent.get_active_project_or_raise().project_root) / rel
        abs_path.write_text("A = 1\nB = 2\nC = 3\nD = 4\n")
        try:
            python_serena_agent.reset_language_server_manager()
        except Exception:
            pass
        try:
            yield rel, abs_path
        finally:
            if abs_path.exists():
                abs_path.unlink()
            try:
                python_serena_agent.reset_language_server_manager()
            except Exception:
                pass

    def test_one_based_line_read_is_the_line_written(self, python_serena_agent, sandbox) -> None:
        rel, abs_path = sandbox
        tool = python_serena_agent.get_tool(CursorReplaceRangeTool)
        # 1-based line 2 is "B = 2"; replacing [2,2] edits exactly that line
        tool.apply(relative_path=rel, start_line=2, end_line=2, body="B = 22\n", expect_version="*")
        assert abs_path.read_text() == "A = 1\nB = 22\nC = 3\nD = 4\n"

    def test_soundness_out_of_band_change_refuses_write(self, python_serena_agent, sandbox) -> None:
        rel, abs_path = sandbox
        version = read_file_version(python_serena_agent.get_active_project_or_raise().project_root, rel)
        abs_path.write_text("A = 1\nCHANGED\nC = 3\nD = 4\n")  # changes under the caller
        tool = python_serena_agent.get_tool(CursorReplaceRangeTool)
        result = tool.apply(relative_path=rel, start_line=1, end_line=1, body="EDIT\n", expect_version=version)
        assert "Stale" in result
        assert abs_path.read_text() == "A = 1\nCHANGED\nC = 3\nD = 4\n"  # refused write touched nothing
