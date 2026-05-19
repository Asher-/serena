"""
Unit tests for :mod:`serena.util.watchdog`.

The watchdog runs a daemon thread that samples own-process CPU and writes
all-thread tracebacks on sustained spin. Tests drive the loop with a scripted
CPU sampler and a fake traceback dumper so the loop's behaviour is observed
without spawning a real high-CPU workload and without touching any file
outside the test's tmp directory.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable

import pytest

from serena.util.watchdog import (
    WatchdogConfig,
    WedgeWatchdog,
    _FaulthandlerTracebackDumper,
)


class _ScriptedSampler:
    """A :class:`CpuSampler` that returns values from a list, then signals exhaustion."""

    def __init__(self, values: list[float]) -> None:
        # initial scripted values plus an event for the test harness to await
        self._values = list(values)
        self._lock = threading.Lock()
        self._exhausted = threading.Event()
        self._calls = 0

    def __call__(self) -> float:
        # serve scripted values first, then return 0 (below threshold) and
        # signal exhaustion so the test can stop the watchdog.
        with self._lock:
            self._calls += 1
            if self._values:
                return self._values.pop(0)
            self._exhausted.set()
            return 0.0

    @property
    def call_count(self) -> int:
        with self._lock:
            return self._calls

    def wait_for_exhaustion(self, timeout: float = 5.0) -> bool:
        return self._exhausted.wait(timeout)


def _make_config(tmp_path: Path, **overrides) -> WatchdogConfig:
    """Build a :class:`WatchdogConfig` rooted at ``tmp_path`` for hermetic tests."""
    defaults = dict(
        cpu_pct_threshold=80.0,
        consecutive_samples=3,
        sample_interval_s=0.0,
        cooldown_after_dump_s=0.0,
        dump_retention=5,
        dump_dir=tmp_path,
    )
    defaults.update(overrides)
    return WatchdogConfig(**defaults)


def _make_recording_dumper() -> tuple[list[Path], Callable[[Path], None]]:
    """Return ``(recorded_paths, dumper)`` -- a fake dumper that records its writes."""
    recorded: list[Path] = []

    def dumper(path: Path) -> None:
        recorded.append(path)
        path.write_text("Current thread 0x0000:\n  faked stack\n")

    return recorded, dumper


class TestWatchdogConfig:
    """Verify env-driven configuration."""

    def test_defaults_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # unset values fall through to the dataclass defaults documented in
        # the plan (80%, 3 samples, 30s interval, 5 retained dumps).
        for name in (
            "SERENA_WEDGE_CPU_PCT",
            "SERENA_WEDGE_CONSECUTIVE_SAMPLES",
            "SERENA_WEDGE_SAMPLE_INTERVAL_S",
            "SERENA_WEDGE_DUMP_RETENTION",
        ):
            monkeypatch.delenv(name, raising=False)

        config = WatchdogConfig.from_env()

        assert config.cpu_pct_threshold == 80.0
        assert config.consecutive_samples == 3
        assert config.sample_interval_s == 30.0
        assert config.cooldown_after_dump_s == 300.0
        assert config.dump_retention == 5

    def test_env_overrides_are_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # honour each documented override; types are float / int as appropriate.
        monkeypatch.setenv("SERENA_WEDGE_CPU_PCT", "50.5")
        monkeypatch.setenv("SERENA_WEDGE_CONSECUTIVE_SAMPLES", "5")
        monkeypatch.setenv("SERENA_WEDGE_SAMPLE_INTERVAL_S", "10")
        monkeypatch.setenv("SERENA_WEDGE_DUMP_RETENTION", "9")

        config = WatchdogConfig.from_env()

        assert config.cpu_pct_threshold == 50.5
        assert config.consecutive_samples == 5
        assert config.sample_interval_s == 10.0
        assert config.dump_retention == 9

    def test_malformed_env_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # a malformed value must not crash; the dataclass default takes over.
        monkeypatch.setenv("SERENA_WEDGE_CPU_PCT", "not-a-float")
        monkeypatch.setenv("SERENA_WEDGE_CONSECUTIVE_SAMPLES", "not-an-int")

        config = WatchdogConfig.from_env()

        assert config.cpu_pct_threshold == 80.0
        assert config.consecutive_samples == 3


class TestWedgeWatchdog:
    """Verify the loop's threshold, consecutive, dump, prune, and cooldown semantics."""

    def test_no_dump_when_all_samples_below_threshold(self, tmp_path: Path) -> None:
        # five samples below threshold: count never reaches the consecutive bar,
        # so the dumper is never called and no sidecar appears.
        sampler = _ScriptedSampler([10.0, 20.0, 30.0, 40.0, 50.0])
        recorded, dumper = _make_recording_dumper()

        watchdog = WedgeWatchdog(
            config=_make_config(tmp_path),
            cpu_sampler=sampler,
            traceback_dumper=dumper,
        )
        watchdog.start()
        try:
            assert sampler.wait_for_exhaustion(timeout=5.0)
            time.sleep(0.1)
        finally:
            watchdog.stop(timeout=2.0)

        assert recorded == []
        assert watchdog.dump_count == 0
        assert list(tmp_path.glob("wedge-*.txt")) == []

    def test_dump_fires_after_threshold_and_consecutive(self, tmp_path: Path) -> None:
        # three below-threshold then three at-threshold: the third over-threshold
        # sample triggers exactly one dump.
        sampler = _ScriptedSampler([10.0, 20.0, 50.0, 90.0, 95.0, 99.0])
        recorded, dumper = _make_recording_dumper()

        watchdog = WedgeWatchdog(
            config=_make_config(tmp_path),
            cpu_sampler=sampler,
            traceback_dumper=dumper,
        )
        watchdog.start()
        try:
            assert sampler.wait_for_exhaustion(timeout=5.0)
            time.sleep(0.1)
        finally:
            watchdog.stop(timeout=2.0)

        assert len(recorded) == 1
        assert recorded[0].parent == tmp_path
        assert recorded[0].name.startswith("wedge-") and recorded[0].name.endswith(".txt")
        assert watchdog.dump_count == 1
        assert watchdog.last_dump_path == recorded[0]

    def test_streak_interrupted_resets_count(self, tmp_path: Path) -> None:
        # 90, 95 (count=2), 30 (count=0), 99, 99 (count=2): never reaches 3, no dump.
        sampler = _ScriptedSampler([90.0, 95.0, 30.0, 99.0, 99.0])
        recorded, dumper = _make_recording_dumper()

        watchdog = WedgeWatchdog(
            config=_make_config(tmp_path),
            cpu_sampler=sampler,
            traceback_dumper=dumper,
        )
        watchdog.start()
        try:
            assert sampler.wait_for_exhaustion(timeout=5.0)
            time.sleep(0.1)
        finally:
            watchdog.stop(timeout=2.0)

        assert recorded == []
        assert watchdog.dump_count == 0

    def test_sidecar_file_appears_under_dump_dir(self, tmp_path: Path) -> None:
        # the default traceback dumper writes faulthandler output to the sidecar
        # path; verify the file exists and the file format starts with the
        # 'Current thread' marker that the launchd wrapper greps for.
        sampler = _ScriptedSampler([99.0, 99.0, 99.0])

        watchdog = WedgeWatchdog(
            config=_make_config(tmp_path),
            cpu_sampler=sampler,
            traceback_dumper=_FaulthandlerTracebackDumper(),
        )
        watchdog.start()
        try:
            assert sampler.wait_for_exhaustion(timeout=5.0)
            time.sleep(0.1)
        finally:
            watchdog.stop(timeout=2.0)

        sidecars = list(tmp_path.glob("wedge-*.txt"))
        assert len(sidecars) == 1
        content = sidecars[0].read_text()
        # faulthandler always emits a 'Current thread' header for the current
        # python thread; the marker is what the launchd wrapper greps for.
        assert "Current thread" in content

    def test_prune_keeps_only_most_recent_dumps(self, tmp_path: Path) -> None:
        # pre-populate seven wedge files with strictly ascending mtimes, then
        # call the prune helper. Retention=3 keeps the three newest.
        for i in range(7):
            f = tmp_path / f"wedge-fixed-{i}.txt"
            f.write_text(f"dump {i}")
            os.utime(f, (1_000_000 + i, 1_000_000 + i))

        watchdog = WedgeWatchdog(
            config=_make_config(tmp_path, dump_retention=3),
            cpu_sampler=lambda: 0.0,
            traceback_dumper=lambda p: None,
        )

        watchdog._prune_old_dumps()

        remaining = {p.name for p in tmp_path.glob("wedge-*.txt")}
        assert remaining == {"wedge-fixed-4.txt", "wedge-fixed-5.txt", "wedge-fixed-6.txt"}

    def test_start_is_idempotent(self, tmp_path: Path) -> None:
        # a second start while running must be a no-op; the watchdog must still
        # be cleanly stoppable afterwards.
        sampler = _ScriptedSampler([])

        watchdog = WedgeWatchdog(
            config=_make_config(tmp_path),
            cpu_sampler=sampler,
            traceback_dumper=lambda p: None,
        )
        watchdog.start()
        try:
            assert watchdog.is_running()
            watchdog.start()  # idempotent
            assert watchdog.is_running()
        finally:
            watchdog.stop(timeout=2.0)
        assert not watchdog.is_running()

    def test_stop_unblocks_long_sleep(self, tmp_path: Path) -> None:
        # the loop must use Event.wait so a stop() during a long sample interval
        # exits promptly rather than hanging until the next sample.
        sampler = _ScriptedSampler([0.0])
        watchdog = WedgeWatchdog(
            config=_make_config(tmp_path, sample_interval_s=60.0),
            cpu_sampler=sampler,
            traceback_dumper=lambda p: None,
        )
        watchdog.start()
        # let one sample complete before stopping so the loop enters _sleep_or_stop.
        sampler.wait_for_exhaustion(timeout=2.0)
        time.sleep(0.05)

        before = time.monotonic()
        watchdog.stop(timeout=5.0)
        elapsed = time.monotonic() - before

        # the watchdog was about to sleep 60s; stop() must unblock it within
        # a small margin (well under the 60s sleep).
        assert elapsed < 2.0
        assert not watchdog.is_running()

    def test_sampler_exception_pauses_one_interval_without_crashing(self, tmp_path: Path) -> None:
        # a sampler that raises on the first call should not crash the loop;
        # subsequent samples are taken normally.
        call_count = {"n": 0}
        ready = threading.Event()

        def flaky_sampler() -> float:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("psutil hiccup")
            ready.set()
            return 0.0

        watchdog = WedgeWatchdog(
            config=_make_config(tmp_path),
            cpu_sampler=flaky_sampler,
            traceback_dumper=lambda p: None,
        )
        watchdog.start()
        try:
            # the loop must keep going after the exception and call sampler again.
            assert ready.wait(timeout=2.0)
            assert call_count["n"] >= 2
        finally:
            watchdog.stop(timeout=2.0)
