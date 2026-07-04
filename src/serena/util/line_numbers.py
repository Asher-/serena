"""Centralized line-number boundary converters (spec-v2 §5.7).

The cursor surface stores line numbers 0-based internally (the LSP convention),
but every external reference an agent has -- ``cat -n``, editors, compiler
errors, git, GitHub -- is 1-based. This module is the single chokepoint that
translates between the two: display sites convert on the way out
(:func:`to_display_line`) and write tools convert agent arguments on the way in
(:func:`to_internal_line`), so no 0-based integer ever crosses the agent
boundary and no two sites disagree on the base.

This is a boundary converter, not a pervasive ``LineNumber`` type: §5.7 treats
line/column as a display projection over byte offsets, so the conversion lives
at the centralized display/argument chokepoints while the internal 0-based
fields (used for LSP queries and range comparisons) stay untouched. The module
imports nothing from :mod:`serena` so it remains a leaf that
:mod:`serena.util.text_utils`, :mod:`serena.cursor`, and
:mod:`serena.tools.cursor_tools` can all depend on without an import cycle.
"""

from __future__ import annotations


def to_display_line(internal_line: int) -> int:
    """Convert an internal 0-based line index to its 1-based display number.

    :param internal_line: the 0-based line index (LSP / internal convention).
    :return: the 1-based line number an agent sees (matches ``cat -n``).
    """
    return internal_line + 1


def to_internal_line(display_line: int) -> int:
    """Convert a 1-based agent line number to the internal 0-based index.

    Inverse of :func:`to_display_line`; applied at write-tool argument entry so
    the internal editing layer keeps its 0-based contract.

    :param display_line: the 1-based line number supplied by the agent.
    :return: the 0-based line index for internal use.
    """
    return display_line - 1


def format_line_range(internal_start: int, internal_end: int) -> str:
    """Render an internal 0-based inclusive span as a 1-based display range.

    Collapses to a single number when the span covers one line, mirroring the
    ``start-end`` / ``start`` shapes the anchor and citation sites emit.

    :param internal_start: the 0-based first line of the span (inclusive).
    :param internal_end: the 0-based last line of the span (inclusive).
    :return: ``"start-end"`` in 1-based form, or ``"start"`` when the span is a
        single line.
    """
    start, end = to_display_line(internal_start), to_display_line(internal_end)
    return str(start) if start == end else f"{start}-{end}"
