import re
import threading
import time
from pathlib import Path

import pytest

from serena.config.serena_config import DEFAULT_TOOL_TIMEOUT
from serena.util import text_utils
from serena.util.text_utils import search_files, search_text

# the pattern whose match stalled the whole daemon on 2026-11-13 (bug://serena/cursor/grep/regex/backtracking/daemon/stall)
STALLING_PATTERN = r"for p in .*:\n(.*\n){0,12}?.*content\(name\)"

# the stalling pattern's single-line form, and a line whose matching runs far past a fraction of a second
STALLING_LINE_PATTERN = r"for p in .*:(.*;){0,12}?.*content\(name\)"
STALLING_LINE = "".join(f"for p in items_{i}: value = p.name; total += value; " for i in range(16))


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
        # the refusal can arrive seconds after the budget expires (up to about 15 s was observed under load)
        assert elapsed < DEFAULT_TOOL_TIMEOUT / 4
        # a thread starved of the interpreter for the whole search would not have ticked at all
        assert ticker.ticks >= 0.25 * elapsed / _Ticker.INTERVAL_SECONDS

    def test_a_glob_past_the_budget_is_refused_naming_the_glob_as_sent(self) -> None:
        # a glob whose translated regex searches a long line in about quadratic time, finding no match
        glob = "?*" * 13 + "c"

        with pytest.raises(text_utils.PatternTimeoutError) as refusal:
            search_text(glob, content="c" + "a" * 50_000, is_glob=True)

        assert refusal.value.pattern == glob

    def test_the_stalling_pattern_still_finds_its_match(self, tmp_path: Path) -> None:
        (tmp_path / "loop.py").write_text("for p in items:\n    name = p.name\n    print(content(name))\n")

        matches = search_files(relative_file_paths=["loop.py"], pattern=STALLING_PATTERN, root_path=str(tmp_path))

        assert [line.line_number for match in matches for line in match.matched_lines] == [0, 1, 2]

    def test_a_line_by_line_search_reports_each_matching_line(self, tmp_path: Path) -> None:
        (tmp_path / "loop.py").write_text("for p in items:\n    name = p.name\n    print(content(name))\n")

        matches = search_files(relative_file_paths=["loop.py"], pattern=r"name\b", root_path=str(tmp_path), multiline=False)

        assert [line.line_number for match in matches for line in match.matched_lines] == [1, 2]

    def test_a_compiled_pattern_is_matched_with_its_own_flags(self) -> None:
        content = "un chat\nthé CAT\nthe CAT"

        ascii_matches = search_text(re.compile(r"\w+ cat", re.IGNORECASE | re.ASCII), content=content)
        unicode_matches = search_text(re.compile(r"\w+ cat", re.IGNORECASE), content=content)

        assert [match.start_line for match in ascii_matches] == [2]
        assert [match.start_line for match in unicode_matches] == [1, 2]


class TestBoundedPattern:
    def test_matches_found_within_the_budget_are_returned_in_order(self) -> None:
        pattern = text_utils.BoundedPattern("x+")

        assert [match.group() for match in pattern.finditer("x-xx-xxx")] == ["x", "xx", "xxx"]

    def test_matching_past_the_budget_is_refused_naming_the_pattern_as_sent(self) -> None:
        pattern = text_utils.BoundedPattern(STALLING_LINE_PATTERN, budget_seconds=0.2, sent_pattern="for p in *content(name)")

        with pytest.raises(text_utils.PatternTimeoutError) as refusal:
            list(pattern.finditer(STALLING_LINE))
        assert refusal.value.pattern == "for p in *content(name)"
        assert refusal.value.budget_seconds == 0.2

    def test_time_between_matches_is_not_charged_to_the_budget(self) -> None:
        pattern = text_utils.BoundedPattern("x", budget_seconds=0.05)
        matches = pattern.finditer("xxx")

        first = next(matches)
        time.sleep(0.1)

        assert [first.group()] + [match.group() for match in matches] == ["x", "x", "x"]

    def test_matching_time_summed_over_many_fast_matches_is_charged_to_the_budget(self) -> None:
        pattern = text_utils.BoundedPattern("x", budget_seconds=0.05)

        with pytest.raises(text_utils.PatternTimeoutError):
            sum(1 for _ in pattern.finditer("x" * 2_000_000))

    def test_the_lines_containing_a_match_are_returned(self) -> None:
        pattern = text_utils.BoundedPattern("b")

        assert pattern.matching_line_indices(["a", "b", "ab", "c"]) == [1, 2]

    def test_a_line_whose_matching_passes_the_budget_is_refused(self) -> None:
        pattern = text_utils.BoundedPattern(STALLING_LINE_PATTERN, budget_seconds=0.2)

        with pytest.raises(text_utils.PatternTimeoutError) as refusal:
            pattern.matching_line_indices(["for p in items: print(content(name))", STALLING_LINE])
        assert refusal.value.pattern == STALLING_LINE_PATTERN

    def test_a_text_pattern_is_matched_with_the_given_flags(self) -> None:
        pattern = text_utils.BoundedPattern("^b.", re.MULTILINE | re.DOTALL)

        assert [match.group() for match in pattern.finditer("a\nb\nc")] == ["b\n"]

    def test_a_compiled_pattern_keeps_its_flags(self) -> None:
        pattern = text_utils.BoundedPattern(re.compile(r"a b  # spaced", re.VERBOSE | re.IGNORECASE))

        assert [match.group() for match in pattern.finditer("xAB ab")] == ["AB", "ab"]

    def test_an_ascii_flag_confines_word_characters_to_ascii(self) -> None:
        ascii_pattern = text_utils.BoundedPattern(re.compile(r"\w+", re.ASCII))
        unicode_pattern = text_utils.BoundedPattern(re.compile(r"\w+"))

        assert [match.group() for match in ascii_pattern.finditer("thé")] == ["th"]
        assert [match.group() for match in unicode_pattern.finditer("thé")] == ["thé"]

    def test_an_invalid_pattern_is_refused_naming_it(self) -> None:
        with pytest.raises(ValueError, match=r"Invalid regular expression 'a\(b'"):
            text_utils.BoundedPattern("a(b")


class TestPatternTimeoutError:
    def test_the_refusal_names_the_pattern_and_the_budget(self) -> None:
        refusal = text_utils.PatternTimeoutError("a.*b", 2.5)

        assert (refusal.pattern, refusal.budget_seconds) == ("a.*b", 2.5)
        assert "'a.*b'" in str(refusal)
        assert "2.5 s" in str(refusal)
