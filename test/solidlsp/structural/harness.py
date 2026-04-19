"""
Shared test helpers for the structural-language layer.

The single contract every backend must satisfy is

    backend.serialize(backend.parse(src)) == src

byte-identically, for every source in the backend's corpus. Backends import
these helpers from their own test modules; there is no central registry.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterator
from pathlib import Path

from solidlsp.structural import StructuralLanguage
from solidlsp.structural.errors import RoundTripViolation


def iter_corpus(corpus_dir: Path, glob: str) -> Iterator[tuple[str, str]]:
    """Yield ``(label, source_text)`` for every file under ``corpus_dir``
    matching ``glob``.

    :param corpus_dir: directory rooted at the backend's own test resources
        (e.g. ``test/solidlsp/python/resources``).
    :param glob: glob pattern relative to ``corpus_dir`` (e.g. ``**/*.py``).
    :return: iterator of ``(relative_label, file_text)`` pairs. Labels are
        stable, human-facing identifiers used in assertion messages.
    """

    # iterate matches deterministically so test ordering is stable
    for path in sorted(corpus_dir.glob(glob)):
        if not path.is_file():
            continue
        yield (
            str(path.relative_to(corpus_dir)),
            path.read_text(encoding="utf-8"),
        )


def assert_round_trip(backend: StructuralLanguage, label: str, source: str) -> None:
    """Assert that ``backend`` round-trips ``source`` byte-identically.

    Parses ``source``, serializes the result, compares bytes. On mismatch,
    raises :class:`~solidlsp.structural.errors.RoundTripViolation` with a
    bounded unified diff for diagnostics.

    :param backend: the :class:`StructuralLanguage` instance under test.
    :param label: a short identifier for the source (path, fixture name) used
        in error output.
    :param source: the text to round-trip.
    """

    # parse and re-serialize
    tree = backend.parse(source)
    rendered = backend.serialize(tree)

    if rendered == source:
        return

    # generate a bounded diff for the failure report; clamp to keep pytest
    # output tractable on large sources
    diff_lines = list(
        difflib.unified_diff(
            source.splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            fromfile=f"{label} (original)",
            tofile=f"{label} (round-tripped)",
            n=2,
        )
    )
    _MAX_DIFF_LINES = 40
    if len(diff_lines) > _MAX_DIFF_LINES:
        diff_lines = diff_lines[:_MAX_DIFF_LINES] + [f"... [{len(diff_lines) - _MAX_DIFF_LINES} more diff lines truncated]\n"]

    raise RoundTripViolation(
        language_key=backend.language_key,
        diff_summary="".join(diff_lines),
    )
