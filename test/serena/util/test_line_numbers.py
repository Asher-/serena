"""
Unit tests for the centralized line-number boundary converters
(:mod:`serena.util.line_numbers`).

These pin the single source of truth for the 1-based line-number contract
(spec-v2 §5.7): ``to_display_line`` / ``to_internal_line`` are exact inverses,
and ``format_line_range`` renders a 1-based inclusive range that collapses to a
single number when the range spans one line.
"""

from __future__ import annotations

from serena.util.line_numbers import format_line_range, to_display_line, to_internal_line


class TestConverters:
    """The 0-based-internal <-> 1-based-display converter pair."""

    def test_to_display_line_shifts_zero_based_to_one_based(self) -> None:
        """The internal 0-based first line maps to the 1-based cat -n first line."""
        assert to_display_line(0) == 1
        assert to_display_line(199) == 200

    def test_to_internal_line_shifts_one_based_to_zero_based(self) -> None:
        """A 1-based agent line maps back to the internal 0-based index."""
        assert to_internal_line(1) == 0
        assert to_internal_line(200) == 199

    def test_converters_are_exact_inverses_over_a_range(self) -> None:
        """Round-tripping through either converter is the identity."""
        for internal in range(0, 500):
            assert to_internal_line(to_display_line(internal)) == internal
        for display in range(1, 500):
            assert to_display_line(to_internal_line(display)) == display


class TestFormatLineRange:
    """1-based inclusive range rendering."""

    def test_multi_line_range_renders_start_dash_end(self) -> None:
        """A multi-line internal span renders as ``start-end`` in 1-based form."""
        assert format_line_range(199, 236) == "200-237"

    def test_single_line_range_collapses_to_one_number(self) -> None:
        """When start == end the range collapses to the single 1-based number."""
        assert format_line_range(5, 5) == "6"
