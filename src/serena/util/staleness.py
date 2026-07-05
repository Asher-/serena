"""Optimistic-concurrency staleness primitives (spec-v2 §5.5).

The compare-and-swap gate every cursor write tool consults before mutating. The versioning
currency is the whole-file content hash (:func:`content_version`): a token that binds
POSITION and content at once -- any change to the file's bytes, whether inside the region a
caller means to edit or above it (shifting the region's line numbers), changes the token, so
a caller working from a stale view is caught before it writes. A benign ``touch`` that leaves
the bytes identical does NOT change the token, so it never strands a valid write (liveness).
The token is byte-based and computed identically by ``stat``, the read projections, and this
gate, so all three agree on a file's version (the staleness-base invariant).

The token and the typed :class:`StaleVersion` mismatch state are shared with the filesystem
lifecycle mutators; they are re-exported from :mod:`serena.util.file_lifecycle` where those
mutators live, and owned here as the §5.5 concurrency vocabulary.
"""

from __future__ import annotations

import os

from serena.util.file_lifecycle import StaleVersion, content_version

__all__ = ["UNCONDITIONAL", "StaleVersion", "check_stale", "content_version", "read_file_version"]

# the explicit "write unconditionally" request (spec-v2 §5.5: the default write is
# conditional; an unconditional overwrite must be asked for by name). A caller passes this
# as ``expect_version`` to bypass the compare-and-swap and own the clobber risk.
UNCONDITIONAL = "*"


def _abs(project_root: str, relative_path: str) -> str:
    """Resolve a project-relative path against the root (read-root == write-root)."""
    return os.path.join(project_root, relative_path)


def read_file_version(project_root: str, relative_path: str) -> str | None:
    """The current content version of a file, or ``None`` when it cannot be read.

    Reads the RAW bytes (never a decoded round-trip, whose re-encoding may not reproduce the
    on-disk bytes) so the token matches the one ``stat`` reports and the read projections show
    -- the staleness-base invariant.

    :param project_root: the project root the relative path resolves against.
    :param relative_path: the file to fingerprint, relative to the root.
    :return: :func:`content_version` of the file's bytes, or ``None`` when the file is missing
        or unreadable (a deleted / renamed-away target -- a cursor-fate signal).
    """
    try:
        with open(_abs(project_root, relative_path), "rb") as handle:
            return content_version(handle.read())
    except OSError:
        return None


def check_stale(
    project_root: str,
    relative_path: str,
    expect_version: str,
    current_bytes: bytes | None = None,
) -> StaleVersion | None:
    """The compare-and-swap gate: decide whether a write may proceed (spec-v2 §5.5).

    :param project_root: the project root the relative path resolves against.
    :param relative_path: the file about to be written.
    :param expect_version: the version the caller believes the file is at (the token a read
        projection or ``stat`` handed it). The sentinel :data:`UNCONDITIONAL` bypasses the
        check -- the explicit, opt-in unconditional write.
    :param current_bytes: the region's current bytes to hand back on a mismatch as the recovery
        ramp; when ``None`` the whole file's current bytes are used.
    :return: ``None`` when the write may proceed (the versions match, or the write is
        unconditional); a :class:`StaleVersion` carrying the current bytes when the file changed
        under the caller -- or was deleted / renamed away (``actual == ""``, a cursor-fate gone
        state). On a non-``None`` result the caller MUST NOT write.
    """
    # the explicit unconditional escape: no comparison, the caller owns the clobber risk
    if expect_version == UNCONDITIONAL:
        return None

    # read the target's current bytes; a missing file is a typed cursor-fate 'gone' state
    # (deleted or renamed away) rather than a silent write against nothing
    try:
        with open(_abs(project_root, relative_path), "rb") as handle:
            raw = handle.read()
    except OSError:
        return StaleVersion(relative_path, expect_version, "", current_bytes if current_bytes is not None else b"")

    # compare-and-swap: an actual-vs-expected mismatch strands the write and hands back the
    # current bytes so the caller can re-read, re-base its intent, and retry
    actual = content_version(raw)
    if actual != expect_version:
        return StaleVersion(relative_path, expect_version, actual, current_bytes if current_bytes is not None else raw)
    return None
