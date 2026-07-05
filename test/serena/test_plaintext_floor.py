"""Unit tests for the universal plaintext floor (spec-v2 §5.1 rung3 / §5.9).

The floor is a pure ``bytes -> PlaintextView`` renderer: it does no I/O, so these
tests drive it with in-memory bytes and assert the typed, never-raising projection.
"""

from solidlsp.structural.backends.plaintext import PlaintextFloor


class TestPlaintextFloorRender:
    """``PlaintextFloor.render`` classifies bytes into a typed view without raising."""

    def setup_method(self) -> None:
        self.floor = PlaintextFloor()

    def test_utf8_text_roundtrips_byte_exact(self) -> None:
        data = b"line one\nline two\n"
        view = self.floor.render(data, "notes.txt")
        assert view.raw_bytes == data
        assert view.text == "line one\nline two\n"
        assert view.text.encode("utf-8") == data  # zero unreadable bytes (spec §5.10 R1)
        assert view.encoding == "utf-8"
        assert view.is_binary is False
        assert view.byte_size == len(data)
        assert view.line_count == 2
        assert view.eol == "LF"
        assert view.trailing_newline is True

    def test_no_trailing_newline_reported(self) -> None:
        view = self.floor.render(b"abc", "a.env")
        assert view.trailing_newline is False
        assert view.line_count == 1
        assert view.eol == "none"

    def test_crlf_detected_and_preserved(self) -> None:
        view = self.floor.render(b"a\r\nb\r\n", "w.txt")
        assert view.eol == "CRLF"
        assert view.trailing_newline is True
        assert view.text.encode("utf-8") == b"a\r\nb\r\n"

    def test_mixed_eol_detected(self) -> None:
        view = self.floor.render(b"a\r\nb\nc", "m.txt")
        assert view.eol == "mixed"

    def test_empty_file_is_typed_state(self) -> None:
        view = self.floor.render(b"", ".gitignore")
        assert view.byte_size == 0
        assert view.line_count == 0
        assert view.is_binary is False
        assert self.floor.describe(view) == "empty (0 bytes)"

    def test_binary_is_typed_state_never_raises(self) -> None:
        data = b"\x89PNG\r\n\x00\x01\x02"
        view = self.floor.render(data, "logo.bin")
        assert view.is_binary is True
        assert view.text is None
        assert view.encoding == "binary"
        assert view.raw_bytes == data
        assert self.floor.describe(view) == f"binary, {len(data)} bytes"

    def test_non_utf8_declares_replacement_never_raises(self) -> None:
        data = b"\xff\xfeABC"  # invalid utf-8, no NUL -> text with declared replacement
        view = self.floor.render(data, "weird.txt")
        assert view.is_binary is False
        assert view.encoding == "utf-8 (replaced)"
        assert view.text is not None
        assert view.raw_bytes == data

    def test_describe_text_carries_shape_and_encoding(self) -> None:
        desc = self.floor.describe(self.floor.render(b"one\ntwo\nthree\n", "n.txt"))
        assert "3 lines" in desc
        assert "byte" in desc
        assert "utf-8" in desc
        assert "LF" in desc
        assert "trailing newline" in desc

    def test_describe_singular_line_and_byte(self) -> None:
        assert "1 line," in self.floor.describe(self.floor.render(b"x", "one.txt"))
        assert "1 byte," in self.floor.describe(self.floor.render(b"x", "one.txt"))

    def test_not_found_view_is_typed(self) -> None:
        view = self.floor.not_found_view("nope.txt")
        assert view.exists is False
        assert view.error == "not found"
        assert view.text is None

    def test_error_view_is_typed(self) -> None:
        view = self.floor.error_view("locked.txt", "Permission denied")
        assert view.exists is True
        assert view.error == "Permission denied"

    def test_acceptance_fixtures_roundtrip(self) -> None:
        # T4 acceptance: round-trip on .txt / .env / LICENSE / .gitignore
        cases = {
            "notes.txt": b"a note\nwith two lines\n",
            "config.env": b"KEY=value\nOTHER=2\n",
            "LICENSE": b"All rights reserved.\n",
            ".gitignore": b"*.pyc\n__pycache__/\n",
        }
        for path, data in cases.items():
            view = self.floor.render(data, path)
            assert view.text is not None
            assert view.text.encode("utf-8") == data  # zero unreadable bytes
            assert self.floor.describe(view)  # a descriptor always renders
