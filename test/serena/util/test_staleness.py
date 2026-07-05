"""Unit tests for the optimistic-concurrency staleness primitives (spec-v2 §5.5).

Covers the whole-file compare-and-swap token and gate the cursor write tools consult:
determinism + byte-sensitivity of the token (staleness-base), the current-bytes recovery
ramp on a mismatch (soundness), the unconditional escape, the deleted-file cursor-fate
signal, and the mtime-vs-content distinction (liveness).
"""

import os
from pathlib import Path

from serena.util.staleness import UNCONDITIONAL, check_stale, content_version, read_file_version


class TestContentVersion:
    """The token itself: a deterministic, byte-sensitive 16-hex fingerprint."""

    def test_is_deterministic_and_16_hex(self) -> None:
        version = content_version(b"hello world\n")
        assert version == content_version(b"hello world\n")
        assert len(version) == 16
        assert all(char in "0123456789abcdef" for char in version)

    def test_differs_on_a_one_byte_change(self) -> None:
        assert content_version(b"abc") != content_version(b"abd")


class TestReadFileVersion:
    """The file-level token: hashes the raw bytes so it agrees with stat + read projections."""

    def test_matches_content_version_of_the_raw_bytes(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_bytes(b"data\n")
        assert read_file_version(str(tmp_path), "f.txt") == content_version(b"data\n")

    def test_missing_file_is_none(self, tmp_path: Path) -> None:
        assert read_file_version(str(tmp_path), "nope.txt") is None


class TestCheckStale:
    """The compare-and-swap gate: permit, refuse-with-ramp, bypass, and gone."""

    def test_matching_version_permits_the_write(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_bytes(b"one\n")
        version = read_file_version(str(tmp_path), "f.txt")
        assert version is not None
        assert check_stale(str(tmp_path), "f.txt", version) is None

    def test_mismatch_refuses_and_hands_back_current_bytes(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_bytes(b"one\n")
        stale = check_stale(str(tmp_path), "f.txt", "deadbeefdeadbeef")
        assert stale is not None
        assert stale.expected == "deadbeefdeadbeef"
        assert stale.actual == content_version(b"one\n")
        assert stale.current_bytes == b"one\n"

    def test_unconditional_sentinel_bypasses_the_check(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_bytes(b"one\n")
        assert check_stale(str(tmp_path), "f.txt", UNCONDITIONAL) is None

    def test_deleted_file_is_a_typed_gone_state(self, tmp_path: Path) -> None:
        stale = check_stale(str(tmp_path), "gone.txt", "deadbeefdeadbeef")
        assert stale is not None
        assert stale.actual == ""  # gone: no current version to report

    def test_benign_touch_does_not_strand_a_valid_write(self, tmp_path: Path) -> None:
        path = tmp_path / "f.txt"
        path.write_bytes(b"one\n")
        version = read_file_version(str(tmp_path), "f.txt")
        assert version is not None
        os.utime(path, (0, 0))  # bump mtime only; the bytes are unchanged
        assert check_stale(str(tmp_path), "f.txt", version) is None  # liveness
