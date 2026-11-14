import threading
import time
from pathlib import Path

import pytest

from serena.config.serena_config import DEFAULT_TOOL_TIMEOUT
from serena.util import text_utils
from serena.util.text_utils import search_files

# the pattern whose match stalled the whole daemon on 2026-11-13 (bug://serena/cursor/grep/regex/backtracking/daemon/stall)
STALLING_PATTERN = r"for p in .*:\n(.*\n){0,12}?.*content\(name\)"


class _Ticker:
    """
    A thread counting ticks at a fixed interval, which advances only while it is allowed to run.
    """

    INTERVAL_SECONDS = 0.01

    def __init__(self) -> None:
        self.ticks = 0
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stopped.wait(self.INTERVAL_SECONDS):
            self.ticks += 1

    def __enter__(self) -> "_Ticker":
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stopped.set()
        self._thread.join()


class TestRegexBudget:
    NUM_LOOPS = 10
    """the number of loops in the searched file, which makes the stalling pattern match for longer than its budget"""

    def test_a_backtracking_pattern_is_refused_within_its_budget_while_other_threads_run(self, tmp_path: Path) -> None:
        # loops of the stalling pattern's shape that never call content(name)
        (tmp_path / "loops.py").write_text(
            "".join(f"for p in items_{i}:\n    value = p.name\n    total += value\n" for i in range(self.NUM_LOOPS))
        )

        with _Ticker() as ticker:
            start = time.monotonic()
            with pytest.raises(ValueError) as refusal:
                search_files(relative_file_paths=["loops.py"], pattern=STALLING_PATTERN, root_path=str(tmp_path))
            elapsed = time.monotonic() - start

        assert isinstance(refusal.value, text_utils.PatternTimeoutError)
        assert refusal.value.pattern == STALLING_PATTERN
        assert STALLING_PATTERN in str(refusal.value)
        assert elapsed < DEFAULT_TOOL_TIMEOUT
        # a thread starved of the interpreter for the whole search would not have ticked at all
        assert ticker.ticks >= 0.25 * elapsed / _Ticker.INTERVAL_SECONDS

    def test_the_stalling_pattern_still_finds_its_match(self, tmp_path: Path) -> None:
        (tmp_path / "loop.py").write_text("for p in items:\n    name = p.name\n    print(content(name))\n")

        matches = search_files(relative_file_paths=["loop.py"], pattern=STALLING_PATTERN, root_path=str(tmp_path))

        assert [line.line_number for match in matches for line in match.matched_lines] == [0, 1, 2]

    def test_a_line_by_line_search_reports_each_matching_line(self, tmp_path: Path) -> None:
        (tmp_path / "loop.py").write_text("for p in items:\n    name = p.name\n    print(content(name))\n")

        matches = search_files(relative_file_paths=["loop.py"], pattern=r"name\b", root_path=str(tmp_path), multiline=False)

        assert [line.line_number for match in matches for line in match.matched_lines] == [1, 2]


class TestBoundedPattern:
    def test_matches_found_within_the_budget_are_returned_in_order(self) -> None:
        pattern = text_utils.BoundedPattern("x+")

        assert [match.group() for match in pattern.finditer("x-xx-xxx")] == ["x", "xx", "xxx"]

    def test_matches_still_sought_once_the_budget_is_spent_are_refused(self) -> None:
        pattern = text_utils.BoundedPattern("x", budget_seconds=0.05)
        matches = pattern.finditer("xxx")

        next(matches)
        time.sleep(0.1)

        with pytest.raises(text_utils.PatternTimeoutError) as refusal:
            next(matches)
        assert refusal.value.pattern == "x"
        assert refusal.value.budget_seconds == 0.05

    def test_the_lines_containing_a_match_are_returned(self) -> None:
        pattern = text_utils.BoundedPattern("b")

        assert pattern.matching_line_indices(["a", "b", "ab", "c"]) == [1, 2]

    def test_lines_left_to_match_once_the_budget_is_spent_are_refused(self) -> None:
        pattern = text_utils.BoundedPattern("b", budget_seconds=0)

        with pytest.raises(text_utils.PatternTimeoutError) as refusal:
            pattern.matching_line_indices(["a", "b"])
        assert refusal.value.pattern == "b"

    def test_an_invalid_pattern_is_refused_naming_it(self) -> None:
        with pytest.raises(ValueError, match=r"Invalid regular expression 'a\(b'"):
            text_utils.BoundedPattern("a(b")
