"""Huge-file windowing primitives (spec-v2 §5.6).

A body-returning read must never dump an unbounded file and never silently
refuse one: it windows the body and ALWAYS emits a descriptor -- present even
when nothing was truncated, so the absence of truncation is never ambiguous
(the "announce" invariant). Paging is a plain "give me the rest": a truncated
window hands back a base64 continuation token carrying ``{path,
next_offset_line, version}``; the caller passes it straight back to read the
next page (no offset arithmetic), and because the token carries the T7 content
version, paging a file that changed underneath is caught as stale.

The module is pure and does no I/O: :func:`window_body` operates on already-read
text, so it stays trivially testable and the byte-access boundary stays in the
cursor manager. Windowing is **line-granular** over ``splitlines(keepends=True)``
units, which makes both boundary guarantees fall out by construction -- a
window edge is always between whole lines, so a CRLF pair is never split and a
multibyte codepoint (always within one line) is never split. Concatenating the
display lines of successive pages reconstructs the body line-for-line
(round-trip across seams).
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass

__all__ = [
    "ContinuationToken",
    "InvalidContinuation",
    "WindowDescriptor",
    "WindowRequest",
    "WindowedBody",
    "decode_continuation",
    "encode_continuation",
    "window_body",
]


class InvalidContinuation(ValueError):
    """A continuation token could not be decoded (malformed / not a windowing token)."""


@dataclass(frozen=True)
class ContinuationToken:
    """The decoded payload of a continuation token (spec-v2 §5.6).

    :ivar path: the project-relative path the token was issued for.
    :ivar next_offset_line: the 0-based body line the next page resumes at.
    :ivar version: the file's content version (spec-v2 §5.5) at issue time, so a
        caller paging a since-changed file is caught as stale rather than served
        bytes from a different revision.
    """

    path: str
    next_offset_line: int
    version: str


def encode_continuation(path: str, next_offset_line: int, version: str) -> str:
    """Encode a paste-safe continuation token carrying ``{path, next_offset_line, version}``.

    :param path: the project-relative path being paged.
    :param next_offset_line: the 0-based body line the next page should resume at.
    :param version: the file's content version at issue time.
    :return: a URL-safe base64 token with no whitespace or path separators leaking through.
    """
    payload = {"path": path, "next_offset_line": int(next_offset_line), "version": version}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_continuation(token: str) -> ContinuationToken:
    """Decode a continuation token, raising :class:`InvalidContinuation` on any malformation.

    :param token: the base64 token a prior truncated read handed back.
    :return: the decoded :class:`ContinuationToken`.
    :raises InvalidContinuation: the token is empty, not base64, not JSON, or is
        missing a required field.
    """
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        payload = json.loads(raw)
        return ContinuationToken(
            path=str(payload["path"]),
            next_offset_line=int(payload["next_offset_line"]),
            version=str(payload["version"]),
        )
    except (binascii.Error, ValueError, KeyError, TypeError) as e:
        raise InvalidContinuation(f"not a valid continuation token: {token!r}") from e


@dataclass(frozen=True)
class WindowRequest:
    """A window request against a body (spec-v2 §5.6); a null request means "the whole body".

    :ivar offset_line: 0-based body line to start the window at (0 = the start).
    :ivar max_lines: cap on the number of body lines shown, or ``None`` for no line cap.
    :ivar max_bytes: cap on the number of body bytes shown, or ``None`` for no byte cap.
        Honored at whole-line granularity: a window always includes at least one line
        even when that line alone exceeds the budget, so paging always makes progress.
    """

    offset_line: int = 0
    max_lines: int | None = None
    max_bytes: int | None = None


@dataclass(frozen=True)
class WindowDescriptor:
    """The always-present window descriptor (spec-v2 §5.6): what was shown, of how much.

    :ivar shown_start_line: 1-based inclusive whole-file line of the first shown line,
        or ``None`` when the window is empty (offset past end / empty body).
    :ivar shown_end_line: 1-based inclusive whole-file line of the last shown line, or ``None``.
    :ivar total_lines: total number of body lines.
    :ivar total_bytes: total byte size of the body.
    :ivar shown_bytes: byte size of the shown window.
    :ivar encoding: the body's declared encoding (for the plaintext floor, the view's).
    :ivar truncated: whether lines remain after the shown window.
    :ivar continuation: the token to read the next page, or ``None`` when nothing remains.
    """

    shown_start_line: int | None
    shown_end_line: int | None
    total_lines: int
    total_bytes: int
    shown_bytes: int
    encoding: str
    truncated: bool
    continuation: str | None

    def render(self) -> str:
        """Render the descriptor as ``lines A-B of N (X of Y bytes), enc, truncated=...``.

        When a continuation token is present it is rendered on its own trailing line so a
        chat UI surfaces it as a copyable handle; the summary line never embeds it.
        """
        shown = "none" if self.shown_start_line is None else f"{self.shown_start_line}-{self.shown_end_line}"
        summary = (
            f"lines {shown} of {self.total_lines} "
            f"({self.shown_bytes} of {self.total_bytes} bytes), "
            f"{self.encoding}, truncated={'true' if self.truncated else 'false'}"
        )
        if self.continuation is not None:
            return f"{summary}\ncontinuation: {self.continuation}"
        return summary


@dataclass(frozen=True)
class WindowedBody:
    """The result of windowing a body: the display lines plus the descriptor.

    :ivar display_lines: the shown lines with their terminators stripped (ready for
        1-based numbering by the caller); empty when the window is empty.
    :ivar numbering_start_line: the 0-based whole-file line of the first display line,
        the base a caller passes to its 1-based line-number converter.
    :ivar descriptor: the always-present :class:`WindowDescriptor`.
    """

    display_lines: list[str]
    numbering_start_line: int
    descriptor: WindowDescriptor


def _strip_terminator(unit: str) -> str:
    r"""Strip a single trailing line terminator (``\r\n`` / ``\n`` / ``\r``) from a keepends unit."""
    if unit.endswith("\r\n"):
        return unit[:-2]
    if unit and unit[-1] in "\r\n":
        return unit[:-1]
    return unit


def window_body(
    text: str,
    *,
    request: WindowRequest,
    path: str,
    version: str | None,
    total_bytes: int | None = None,
    encoding: str = "utf-8",
    line_base: int = 0,
) -> WindowedBody:
    """Window ``text`` at line boundaries and build the always-present descriptor (spec-v2 §5.6).

    :param text: the full body text to window (already decoded).
    :param request: the window bounds; a null request yields the whole body.
    :param path: the project-relative path, embedded in any continuation token.
    :param version: the file's content version, embedded in any continuation token so
        a later page of a changed file is caught as stale.
    :param total_bytes: the body's true byte size; defaults to the UTF-8 length of ``text``
        (the caller passes the plaintext view's raw byte size so the descriptor matches disk).
    :param encoding: the body's declared encoding, for the descriptor.
    :param line_base: the 0-based whole-file line of ``text``'s first line, so a windowed
        sub-body (an LSP symbol or a structural node) reports whole-file coordinates.
    :return: the :class:`WindowedBody`.
    """
    units = text.splitlines(keepends=True)
    total_lines = len(units)
    body_total_bytes = total_bytes if total_bytes is not None else len(text.encode("utf-8"))

    # clamp the start into range; select a contiguous slice bounded by max_lines and
    # max_bytes, always including at least one line so a page never stalls (the byte
    # check is skipped for the first line via the ``end > start`` guard).
    start = max(0, min(request.offset_line, total_lines))
    end = start
    acc_bytes = 0
    while end < total_lines:
        if request.max_lines is not None and (end - start) >= request.max_lines:
            break
        unit_bytes = len(units[end].encode("utf-8"))
        if request.max_bytes is not None and end > start and acc_bytes + unit_bytes > request.max_bytes:
            break
        acc_bytes += unit_bytes
        end += 1

    shown_units = units[start:end]
    display_lines = [_strip_terminator(u) for u in shown_units]
    shown_bytes = len("".join(shown_units).encode("utf-8"))
    truncated = end < total_lines
    continuation = encode_continuation(path, end, version or "") if truncated else None

    if shown_units:
        shown_start_line: int | None = line_base + start + 1
        shown_end_line: int | None = line_base + end
    else:
        shown_start_line = shown_end_line = None

    descriptor = WindowDescriptor(
        shown_start_line=shown_start_line,
        shown_end_line=shown_end_line,
        total_lines=total_lines,
        total_bytes=body_total_bytes,
        shown_bytes=shown_bytes,
        encoding=encoding,
        truncated=truncated,
        continuation=continuation,
    )
    return WindowedBody(display_lines=display_lines, numbering_start_line=line_base + start, descriptor=descriptor)
