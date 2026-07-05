"""Universal plaintext floor: the bottom rung of the resolution ladder (spec-v2 §5.1 rung3).

The :class:`PlaintextFloor` is the terminal fallback that makes *every* target's
bytes renderable through the cursor surface. Unlike the thirteen extension-keyed
:class:`~solidlsp.structural.base.StructuralLanguage` backends -- which are AST
editors (parse / serialize / walk / pattern-rewrite) -- the floor is deliberately a
boring byte renderer: given a chunk of raw bytes it produces a typed, never-raising
:class:`PlaintextView` (a line/size/encoding summary, a byte-exact body, an encoding
state). It is NOT modelled as a ``StructuralLanguage`` because bytes are not an AST;
forcing them through that ABC would be a category error.

The floor performs **no I/O of its own**: its caller (the cursor manager, which already
owns the byte-access boundary) hands it the bytes. That keeps the floor a pure,
trivially-testable transformer and the I/O boundary in one place. The floor is the
analogue of ``ReadRung.PLAINTEXT``: it never raises -- non-UTF-8 bytes, binary content,
and a missing/unreachable target all render as states rather than exceptions
(spec-v2 §5.9), so no lookup ever dead-ends.
"""

from __future__ import annotations

from dataclasses import dataclass

# how many leading bytes to sniff for a NUL when classifying binary content;
# mirrors the git heuristic (a NUL in the first 8 KiB means "treat as binary")
_BINARY_SNIFF_BYTES = 8192


def _plural(n: int) -> str:
    """the plural suffix for a count -- ``""`` for one, ``"s"`` otherwise."""
    return "" if n == 1 else "s"


@dataclass(frozen=True)
class PlaintextView:
    """A typed, never-raising projection of a target the richer rungs do not claim.

    Every field is populated for every target: the view is the plaintext rung's
    total answer, carrying enough to render a summary, a byte-exact body, and the
    encoding/line-ending state (spec-v2 §5.9 edge-case rendered states).

    :ivar relative_path: project-relative path this view describes.
    :ivar raw_bytes: the exact on-disk bytes; the round-trip currency
        (concatenation of a target's addressable regions must reconstruct these).
    :ivar text: the decoded text, or ``None`` when the target is binary.
    :ivar encoding: the declared decode -- ``"utf-8"`` on a clean decode,
        ``"utf-8 (replaced)"`` when undecodable bytes were replaced, or
        ``"binary"`` for binary content.
    :ivar is_binary: whether the target was classified as binary (a NUL in the
        sniff window); a binary target has ``text is None``.
    :ivar byte_size: length of :attr:`raw_bytes`.
    :ivar line_count: number of text lines (``0`` for empty or binary).
    :ivar eol: the line-ending style -- ``"LF"``, ``"CRLF"``, ``"CR"``,
        ``"mixed"``, or ``"none"`` (no line ending present).
    :ivar trailing_newline: whether the last byte is a line ending.
    :ivar exists: whether the target was found on disk.
    :ivar error: a rendered error state (missing / permission), or ``None`` on a
        clean decode. Never an exception -- the floor never raises.
    """

    relative_path: str
    raw_bytes: bytes
    text: str | None
    encoding: str
    is_binary: bool
    byte_size: int
    line_count: int
    eol: str
    trailing_newline: bool
    exists: bool = True
    error: str | None = None


class PlaintextFloor:
    """The universal terminal floor: renders any bytes into a :class:`PlaintextView`, never raising.

    Registered as the ladder's bottom rung; consulted by the cursor surface for
    any target no LSP or structural rung claims, and as the ultimate fallback for a
    target a richer rung fails to parse. Holds no state and performs no I/O.
    """

    def render(self, raw_bytes: bytes, relative_path: str) -> PlaintextView:
        """Render raw bytes into a typed view; never raise.

        :param raw_bytes: the exact on-disk bytes.
        :param relative_path: project-relative path, for display.
        :return: a :class:`PlaintextView`; binary content and non-UTF-8 bytes
            each render as a state rather than raising.
        """
        # binary classification: a NUL in the sniff window means "not text"
        byte_size = len(raw_bytes)
        if b"\x00" in raw_bytes[:_BINARY_SNIFF_BYTES]:
            return PlaintextView(relative_path, raw_bytes, None, "binary", True, byte_size, 0, "none", False)

        # declared UTF-8 decode with a replacement fallback (never a UnicodeDecodeError)
        try:
            text = raw_bytes.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            text = raw_bytes.decode("utf-8", errors="replace")
            encoding = "utf-8 (replaced)"

        # line-ending and shape state so callers can preserve them
        eol = self._detect_eol(raw_bytes)
        trailing_newline = raw_bytes.endswith((b"\n", b"\r"))
        line_count = len(text.splitlines())
        return PlaintextView(relative_path, raw_bytes, text, encoding, False, byte_size, line_count, eol, trailing_newline)

    def not_found_view(self, relative_path: str) -> PlaintextView:
        """A rendered "not found" state -- never an exception."""
        return PlaintextView(relative_path, b"", None, "", False, 0, 0, "none", False, exists=False, error="not found")

    def error_view(self, relative_path: str, error: str) -> PlaintextView:
        """A rendered access-error state (e.g. permission denied) -- never an exception."""
        return PlaintextView(relative_path, b"", None, "", False, 0, 0, "none", False, exists=True, error=error)

    @staticmethod
    def _detect_eol(data: bytes) -> str:
        """Classify the line-ending style from raw bytes.

        :param data: the raw bytes.
        :return: ``"CRLF"`` / ``"LF"`` / ``"CR"`` when a single style is present,
            ``"mixed"`` when several are, ``"none"`` when no line ending occurs.
        """
        crlf = data.count(b"\r\n")
        cr = data.count(b"\r") - crlf
        lf = data.count(b"\n") - crlf
        present = [name for name, count in (("CRLF", crlf), ("CR", cr), ("LF", lf)) if count > 0]
        if not present:
            return "none"
        if len(present) == 1:
            return present[0]
        return "mixed"

    def describe(self, view: PlaintextView) -> str:
        """Render the one-line summary the plaintext overview rung shows.

        :param view: the target's :class:`PlaintextView`.
        :return: a compact ``"N line(s), M byte(s), <encoding>, <eol>, <trailing>"``
            summary; a binary / empty / errored target gets its own short form.
        """
        # error and binary states render as their own terminal descriptors
        if view.error is not None:
            return f"{view.relative_path}: {view.error}"
        if view.is_binary:
            return f"binary, {view.byte_size} byte{_plural(view.byte_size)}"
        if view.byte_size == 0:
            return "empty (0 bytes)"

        # the text descriptor: shape + encoding + line-ending state
        return ", ".join(
            [
                f"{view.line_count} line{_plural(view.line_count)}",
                f"{view.byte_size} byte{_plural(view.byte_size)}",
                view.encoding,
                view.eol,
                "trailing newline" if view.trailing_newline else "no trailing newline",
            ],
        )


__all__ = ["PlaintextFloor", "PlaintextView"]
