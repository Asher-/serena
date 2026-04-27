"""Per-arm tool name allowlists for the cursor-vs-RA pilot.

These mirror the ``fixed_tools`` lists in ``configs/{cursor,ra}_arm.yml`` and are
used by the harness as an integrity check: after spawning the serena MCP server
with the arm's mode YAML applied, the published tool set must equal the arm's
``tool_names`` exactly. A mismatch means the mode wasn't loaded as expected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class Arm:
    """names a paradigm arm and the exact set of tool names that define it."""

    name: str
    tool_names: frozenset[str]


CURSOR_ARM: Final[Arm] = Arm(
    name="cursor",
    tool_names=frozenset(
        {
            "cursor_start",
            "cursor_find",
            "cursor_move",
            "cursor_look",
            "cursor_configure",
            "cursor_overview",
            "cursor_history",
            "cursor_close",
        }
    ),
)

RA_ARM: Final[Arm] = Arm(
    name="ra",
    tool_names=frozenset(
        {
            "find_symbol",
            "find_referencing_symbols",
            "get_symbols_overview",
            "search_for_pattern",
        }
    ),
)

ARMS: Final[dict[str, Arm]] = {a.name: a for a in (CURSOR_ARM, RA_ARM)}
