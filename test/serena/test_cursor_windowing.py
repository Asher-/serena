"""Integration tests for huge-file windowing on the cursor surface (spec-v2 §5.6).

Drives windowing through the plaintext floor cursor (the whole-file body, the primary
huge-file case). Covers the three acceptance invariants -- ``announce`` (a body always
carries a ``window:`` descriptor, even untruncated), ``hw-cover`` (paging via the
continuation token reconstructs the file line-for-line across seams), and ``boundary``
(a CRLF pair or a multibyte codepoint is never split) -- plus the typed-stale outcome of
paging a file that changed under the token.
"""

from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

from serena.cursor import CursorManager


def _manager(tmp_path: Path) -> CursorManager:
    """A CursorManager over ``tmp_path`` with a stub project (default 13-backend registry)."""
    project = MagicMock()
    project.project_root = str(tmp_path)
    project.read_file = MagicMock(side_effect=lambda p: Path(tmp_path / p).read_text(encoding="utf-8"))
    return CursorManager(project)


def _force_lsp_miss():
    """Patch the LSP retriever so ``find_unique`` misses -> start_cursor falls to the floor."""
    retriever = MagicMock()
    retriever.find_unique.side_effect = ValueError("no symbol")
    return patch.object(CursorManager, "_retriever", new_callable=PropertyMock, return_value=retriever)


def _plaintext_cursor(tmp_path: Path, rel: str, data: bytes) -> tuple[CursorManager, str]:
    """Write ``data`` to ``rel`` and land a plaintext cursor on it; return ``(manager, cursor_id)``."""
    (tmp_path / rel).write_bytes(data)
    manager = _manager(tmp_path)
    with _force_lsp_miss():
        cid, _ = manager.start_cursor(rel, relative_path=rel)
    return manager, cid


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
    """The continuation token a truncated read handed back, or ``None`` when nothing remains."""
    for ln in view.splitlines():
        if ln.startswith("continuation: "):
            return ln[len("continuation: ") :]
    return None


# ------------------------------------------------------------------ announce


class TestAnnounce:
    def test_untruncated_body_still_carries_a_descriptor(self, tmp_path: Path) -> None:
        manager, cid = _plaintext_cursor(tmp_path, "n.txt", b"a\nb\nc\n")
        view = manager.format_cursor_view(cid)  # no window params
        assert "window: lines 1-3 of 3" in view
        assert "truncated=false" in view
        assert "continuation:" not in view

    def test_max_lines_truncates_and_offers_a_continuation(self, tmp_path: Path) -> None:
        data = "".join(f"r{i}\n" for i in range(10)).encode("utf-8")
        manager, cid = _plaintext_cursor(tmp_path, "n.txt", data)
        view = manager.format_cursor_view(cid, max_lines=4)
        assert _body_content(view) == ["r0", "r1", "r2", "r3"]
        assert "truncated=true" in view
        assert _continuation(view) is not None

    def test_offset_line_is_1_based(self, tmp_path: Path) -> None:
        data = "".join(f"r{i}\n" for i in range(10)).encode("utf-8")
        manager, cid = _plaintext_cursor(tmp_path, "n.txt", data)
        view = manager.format_cursor_view(cid, offset_line=5, max_lines=2)
        assert _body_content(view) == ["r4", "r5"]  # 1-based line 5 == r4
        assert "lines 5-6 of 10" in view


# ----------------------------------------------------------------- hw-cover


class TestRoundTripAcrossSeams:
    def test_paging_reconstructs_the_file_line_for_line(self, tmp_path: Path) -> None:
        lines = [f"row{i}\n" for i in range(20)]
        manager, cid = _plaintext_cursor(tmp_path, "big.txt", "".join(lines).encode("utf-8"))
        collected: list[str] = []
        view = manager.format_cursor_view(cid, max_lines=5)
        pages = 0
        while True:
            collected.extend(_body_content(view))
            pages += 1
            token = _continuation(view)
            if token is None:
                break
            view = manager.format_cursor_view(cid, continuation=token, max_lines=5)
            assert pages < 100
        assert collected == [ln.rstrip("\n") for ln in lines]
        assert pages == 4  # 5+5+5+5


# ----------------------------------------------------------------- boundary


class TestBoundary:
    def test_crlf_pair_is_never_split_across_pages(self, tmp_path: Path) -> None:
        manager, cid = _plaintext_cursor(tmp_path, "n.txt", b"a\r\nb\r\nc\r\nd\r\n")
        p1 = manager.format_cursor_view(cid, max_lines=2)
        assert _body_content(p1) == ["a", "b"]
        p2 = manager.format_cursor_view(cid, continuation=_continuation(p1), max_lines=2)
        assert _body_content(p2) == ["c", "d"]

    def test_multibyte_codepoint_is_never_split(self, tmp_path: Path) -> None:
        manager, cid = _plaintext_cursor(tmp_path, "n.txt", "café\nnaïve\n".encode())
        view = manager.format_cursor_view(cid, max_lines=1)
        assert _body_content(view) == ["café"]
        assert "�" not in view  # no replacement char introduced


# --------------------------------------------------------- stale paging (spec-v2 5.5 + 5.6)


class TestStalePaging:
    def test_paging_a_changed_file_is_typed_stale_not_stale_bytes(self, tmp_path: Path) -> None:
        path = tmp_path / "n.txt"
        manager, cid = _plaintext_cursor(tmp_path, "n.txt", "".join(f"r{i}\n" for i in range(10)).encode("utf-8"))
        page1 = manager.format_cursor_view(cid, max_lines=4)
        token = _continuation(page1)
        assert token is not None
        # the file changes out-of-band: its content version moves, so the token is stale
        path.write_bytes(b"totally different\n")
        page2 = manager.format_cursor_view(cid, continuation=token, max_lines=4)
        assert "stale window" in page2
        assert "r4" not in page2  # never serves bytes from the token's revision
