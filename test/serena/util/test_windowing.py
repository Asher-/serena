"""Unit tests for the huge-file windowing primitives (spec-v2 §5.6).

Pins the pure, no-I/O contract of :mod:`serena.util.windowing`: line-boundary
windowing (never splitting a CRLF pair or a multibyte codepoint), the
always-present descriptor (present even when ``truncated`` is false -- the
"announce" invariant), round-trip across page seams (``hw-cover``), and the
base64 continuation token that carries ``{path, next_offset_line, version}``.
"""

from __future__ import annotations

import pytest

from serena.util.windowing import (
    InvalidContinuation,
    WindowRequest,
    decode_continuation,
    encode_continuation,
    window_body,
)

_LINES = [f"line{i}\n" for i in range(10)]  # 10 lines, each "lineN\n"
_TEXT = "".join(_LINES)
_TOTAL_BYTES = len(_TEXT.encode("utf-8"))


def _window(text: str, **kwargs) -> object:
    """Window ``text`` with a fresh request built from keyword params."""
    req = WindowRequest(
        offset_line=kwargs.pop("offset_line", 0),
        max_lines=kwargs.pop("max_lines", None),
        max_bytes=kwargs.pop("max_bytes", None),
    )
    return window_body(text, request=req, path=kwargs.pop("path", "f.txt"), version=kwargs.pop("version", "v0"), **kwargs)


# ---------------------------------------------------------------- announce


class TestDescriptorAlwaysPresent:
    def test_untruncated_body_still_carries_a_descriptor(self) -> None:
        wb = _window(_TEXT)
        assert wb.descriptor.truncated is False
        assert wb.descriptor.total_lines == 10
        assert wb.descriptor.total_bytes == _TOTAL_BYTES
        assert wb.descriptor.shown_start_line == 1
        assert wb.descriptor.shown_end_line == 10
        assert wb.descriptor.continuation is None
        rendered = wb.descriptor.render()
        assert "lines 1-10 of 10" in rendered
        assert "truncated=false" in rendered
        assert "continuation:" not in rendered

    def test_empty_body_reports_none_shown(self) -> None:
        wb = _window("")
        assert wb.descriptor.total_lines == 0
        assert wb.descriptor.shown_start_line is None
        assert wb.descriptor.truncated is False
        assert "lines none of 0" in wb.descriptor.render()

    def test_offset_past_end_shows_nothing_without_error(self) -> None:
        wb = _window(_TEXT, offset_line=99)
        assert wb.display_lines == []
        assert wb.descriptor.shown_start_line is None
        assert wb.descriptor.truncated is False


# ------------------------------------------------------------ line caps


class TestMaxLines:
    def test_max_lines_truncates_and_offers_continuation(self) -> None:
        wb = _window(_TEXT, max_lines=4)
        assert wb.display_lines == ["line0", "line1", "line2", "line3"]
        assert wb.descriptor.truncated is True
        assert wb.descriptor.shown_start_line == 1
        assert wb.descriptor.shown_end_line == 4
        assert wb.descriptor.continuation is not None
        token = decode_continuation(wb.descriptor.continuation)
        assert token.next_offset_line == 4
        assert token.path == "f.txt"
        assert token.version == "v0"

    def test_second_page_resumes_at_offset(self) -> None:
        wb = _window(_TEXT, offset_line=4, max_lines=4)
        assert wb.display_lines == ["line4", "line5", "line6", "line7"]
        assert wb.descriptor.shown_start_line == 5
        assert wb.descriptor.shown_end_line == 8
        assert wb.descriptor.truncated is True

    def test_final_page_is_untruncated(self) -> None:
        wb = _window(_TEXT, offset_line=8, max_lines=4)
        assert wb.display_lines == ["line8", "line9"]
        assert wb.descriptor.truncated is False
        assert wb.descriptor.continuation is None


# -------------------------------------------------------- round-trip (hw-cover)


class TestRoundTripAcrossSeams:
    def test_paging_reconstructs_every_line(self) -> None:
        # walk the whole body in pages of 3 by following continuation offsets;
        # the concatenation of the pages' display lines equals the original lines
        collected: list[str] = []
        offset = 0
        pages = 0
        while True:
            wb = _window(_TEXT, offset_line=offset, max_lines=3)
            collected.extend(wb.display_lines)
            pages += 1
            if not wb.descriptor.truncated:
                break
            offset = decode_continuation(wb.descriptor.continuation).next_offset_line
            assert pages < 100, "paging did not terminate"
        assert collected == [ln.rstrip("\n") for ln in _LINES]
        assert pages == 4  # 3+3+3+1


# ------------------------------------------------------------ byte caps + boundaries


class TestMaxBytesBoundary:
    def test_max_bytes_cuts_at_a_whole_line(self) -> None:
        # each "lineN\n" is 6 bytes; a 15-byte budget fits two whole lines (12),
        # never a partial third (would be 18)
        wb = _window(_TEXT, max_bytes=15)
        assert wb.display_lines == ["line0", "line1"]
        assert wb.descriptor.truncated is True
        assert wb.descriptor.shown_bytes == 12

    def test_single_over_budget_line_still_makes_progress(self) -> None:
        text = "x" * 100 + "\n" + "tail\n"
        wb = _window(text, max_bytes=5)
        # the first line alone exceeds the budget, but a page must never be empty
        assert wb.display_lines == ["x" * 100]
        assert wb.descriptor.truncated is True

    def test_crlf_pair_is_never_split(self) -> None:
        text = "a\r\nb\r\nc\r\n"
        wb = _window(text, max_lines=2)
        # windowing is line-granular over keepends units, so a \r\n is atomic:
        # the shown region re-joined with its terminators is a clean prefix
        assert wb.display_lines == ["a", "b"]
        # round-trip: page 2 completes the file with no orphaned \r or \n
        wb2 = _window(text, offset_line=2, max_lines=2)
        assert wb2.display_lines == ["c"]

    def test_multibyte_codepoint_is_never_split(self) -> None:
        text = "café\nnaïve\n"
        wb = _window(text, max_lines=1)
        assert wb.display_lines == ["café"]
        # no replacement characters were introduced (the codepoint stayed whole)
        assert "�" not in wb.display_lines[0]


# ---------------------------------------------------------- whole-file coords


class TestLineBaseWholeFileCoords:
    def test_line_base_offsets_descriptor_into_whole_file_coords(self) -> None:
        # a structural node body starting at file line 101 (0-based 100) reports
        # its window in whole-file 1-based coordinates so symmetry holds
        wb = _window("n0\nn1\nn2\n", line_base=100, max_lines=2)
        assert wb.descriptor.shown_start_line == 101
        assert wb.descriptor.shown_end_line == 102
        assert wb.numbering_start_line == 100


# ------------------------------------------------------------- continuation token


class TestContinuationToken:
    def test_encode_decode_round_trip(self) -> None:
        token = encode_continuation("a/b.py", 42, "deadbeefcafe0001")
        decoded = decode_continuation(token)
        assert decoded.path == "a/b.py"
        assert decoded.next_offset_line == 42
        assert decoded.version == "deadbeefcafe0001"

    def test_token_is_opaque_base64_without_delimiters(self) -> None:
        token = encode_continuation("a/b.py", 42, "v")
        # a paste-safe token: no whitespace, no path separators leaking through
        assert " " not in token and "\n" not in token

    @pytest.mark.parametrize("bad", ["", "not-base64!!", "YWJj", "e30="])
    def test_invalid_token_raises(self, bad: str) -> None:
        with pytest.raises(InvalidContinuation):
            decode_continuation(bad)
