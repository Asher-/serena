"""
Meta-tests for the round-trip harness itself.

Exercises :func:`assert_round_trip` and :func:`iter_corpus` using a tiny
in-memory fake backend. This is the only test in the structural test tree that
does not depend on a real backend \u2014 real backends plug in once Python /
C++ / Swift are wired.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest

from solidlsp.structural import StructuralLanguage
from solidlsp.structural.errors import RoundTripViolation
from solidlsp.structural.kinds import KindName, KindSchema, KindSpec
from solidlsp.structural.names import LogicalName, LogicalNameResolver, NameResolution
from solidlsp.structural.patterns import AstPattern, PatternMatch
from test.solidlsp.structural.harness import assert_round_trip, iter_corpus

# ---- fake backend used only for harness self-tests ------------------------


class _FakeResolver:
    """Logical-name resolver used only by the fake backend below."""

    def parse(self, raw: str) -> LogicalName:
        return LogicalName(parts=tuple(raw.split(".")), raw=raw)

    def resolve(self, name: LogicalName) -> NameResolution:
        return NameResolution(relative_path="/".join(name.parts) + ".fake", source_kind="source", exists=False)


class _FakeBackend(StructuralLanguage):
    """Minimal backend whose AST handle is the raw source string.

    ``parse`` is the identity; ``serialize`` is the identity. Trivially
    round-trips any input. Used here only to exercise the harness; never
    registered as a real language.
    """

    _SCHEMA = KindSchema(
        language_key="fake",
        source_kinds=frozenset({"source"}),
        kinds={
            "source": KindSpec(
                name="source",
                description="test-only source kind",
                attributes=(),
                allowed_parent_kinds=frozenset(),
                allowed_child_kinds=frozenset(),
            ),
        },
    )

    _RESOLVER = _FakeResolver()

    @property
    def language_key(self) -> str:
        return "fake"

    @property
    def kind_schema(self) -> KindSchema:
        return self._SCHEMA

    @property
    def name_resolver(self) -> LogicalNameResolver:
        return self._RESOLVER

    def parse(self, source: str) -> Any:
        return source

    def serialize(self, tree: Any) -> str:
        # string cast keeps the ABC's contract even if a subclass overrides
        # parse() to return something richer
        return str(tree)

    def root_kind(self, tree: Any) -> KindName:
        return "source"

    def walk_symbols(self, tree: Any) -> Iterable[tuple[str, KindName, Any]]:
        return iter(())

    def build_declaration(self, kind: KindName, attributes: Mapping[str, Any], children: Iterable[Any]) -> Any:
        raise NotImplementedError

    def insert_child(self, parent: Any, child: Any, anchor: Any | None = None, position: str = "end") -> Any:
        raise NotImplementedError

    def remove_child(self, parent: Any, child: Any) -> Any:
        raise NotImplementedError

    def compile_pattern(self, pattern_source: str) -> AstPattern:
        raise NotImplementedError

    def find_matches(self, tree: Any, pattern: AstPattern, scope: Any | None = None) -> Iterable[PatternMatch]:
        return iter(())

    def render_replacement(self, replacement_source: str, bindings: Mapping[str, Any]) -> Any:
        raise NotImplementedError

    def apply_replacement(self, tree: Any, match: PatternMatch, replacement: Any) -> Any:
        raise NotImplementedError

    def empty_source(self, source_kind: KindName) -> Any:
        return ""


class _LossyFakeBackend(_FakeBackend):
    """Same as :class:`_FakeBackend` but normalises trailing whitespace on
    serialize, to deliberately violate the round-trip contract.
    """

    def serialize(self, tree: Any) -> str:
        return str(tree).rstrip(" \t")


# ---- harness behaviour ----------------------------------------------------


class TestAssertRoundTrip:
    """Covers the two outcomes of the round-trip invariant."""

    def test_faithful_backend_passes(self) -> None:
        # identity backend never violates the invariant
        assert_round_trip(_FakeBackend(), "sample.fake", "abc\n")

    def test_empty_source_passes(self) -> None:
        # edge case: empty string round-trips
        assert_round_trip(_FakeBackend(), "empty.fake", "")

    def test_lossy_backend_raises(self) -> None:
        # trailing-whitespace normalisation drops the trailing spaces on round-trip
        with pytest.raises(RoundTripViolation) as excinfo:
            assert_round_trip(_LossyFakeBackend(), "trail.fake", "line with trailing space   ")

        # the violation carries a language-key for routing
        assert excinfo.value.language_key == "fake"

        # and a bounded diff the harness formatted
        assert excinfo.value.diff_summary
        assert "trail.fake" in excinfo.value.diff_summary


# ---- corpus iteration -----------------------------------------------------


class TestIterCorpus:
    """Covers :func:`iter_corpus` file discovery and ordering."""

    def test_yields_relative_label_and_contents(self, tmp_path: Path) -> None:
        # two files with different contents
        (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
        (tmp_path / "b.txt").write_text("beta", encoding="utf-8")

        results = dict(iter_corpus(tmp_path, "*.txt"))

        assert results == {"a.txt": "alpha", "b.txt": "beta"}

    def test_ordering_is_stable(self, tmp_path: Path) -> None:
        # creation order is reversed; iteration should still sort
        for name in ("c.txt", "a.txt", "b.txt"):
            (tmp_path / name).write_text(name, encoding="utf-8")

        labels = [label for label, _ in iter_corpus(tmp_path, "*.txt")]

        assert labels == sorted(labels)

    def test_glob_filters_non_matching_files(self, tmp_path: Path) -> None:
        # glob restricts discovery
        (tmp_path / "included.py").write_text("x", encoding="utf-8")
        (tmp_path / "excluded.md").write_text("y", encoding="utf-8")

        labels = [label for label, _ in iter_corpus(tmp_path, "*.py")]

        assert labels == ["included.py"]

    def test_directories_are_skipped(self, tmp_path: Path) -> None:
        # a matching directory must not appear in the yield
        (tmp_path / "sub.txt").mkdir()
        (tmp_path / "real.txt").write_text("x", encoding="utf-8")

        labels = [label for label, _ in iter_corpus(tmp_path, "*.txt")]

        assert labels == ["real.txt"]
