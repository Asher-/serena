"""
CPU spin watchdog for the Serena daemon.

Provides :class:`WedgeWatchdog`, a daemon-thread sampler that writes an
all-thread traceback to a per-event sidecar file under
``~/Library/Logs/serena/`` whenever sustained high own-process CPU is
observed. The sidecar format matches :func:`faulthandler.dump_traceback`
so operators have a single parser surface for both signal-triggered (via
SIGUSR1) and watchdog-triggered evidence.

Designed to be lightweight under steady-state load: samples on a 30 second
cadence by default and only acts on three consecutive over-threshold
samples (=> roughly 90 seconds of sustained spin before a dump), with a
five-minute cooldown after every dump to avoid storming disk if the spin
persists.
"""

from __future__ import annotations

import dataclasses
import faulthandler
import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import psutil

log = logging.getLogger(__name__)


_ENV_CPU_PCT = "SERENA_WEDGE_CPU_PCT"
_ENV_CONSECUTIVE_SAMPLES = "SERENA_WEDGE_CONSECUTIVE_SAMPLES"
_ENV_SAMPLE_INTERVAL_S = "SERENA_WEDGE_SAMPLE_INTERVAL_S"
_ENV_DUMP_RETENTION = "SERENA_WEDGE_DUMP_RETENTION"

_DEFAULT_CPU_PCT_THRESHOLD = 80.0
_DEFAULT_CONSECUTIVE_SAMPLES = 3
_DEFAULT_SAMPLE_INTERVAL_S = 30.0
_DEFAULT_COOLDOWN_AFTER_DUMP_S = 300.0
_DEFAULT_DUMP_RETENTION = 5
_DEFAULT_DUMP_DIR = Path.home() / "Library" / "Logs" / "serena"
_DUMP_FILENAME_GLOB = "wedge-*.txt"


class CpuSampler(Protocol):
    """A callable that returns the current own-process CPU percent (0--100+)."""

    def __call__(self) -> float: ...


class TracebackDumper(Protocol):
    """A callable that writes an all-thread traceback to the given file path."""

    def __call__(self, path: Path) -> None: ...


def _parse_float(name: str, default: float) -> float:
    """Return ``float(os.environ[name])`` or ``default`` on missing/malformed."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("Invalid %s=%r; falling back to default %s", name, raw, default)
        return default


def _parse_int(name: str, default: int) -> int:
    """Return ``int(os.environ[name])`` or ``default`` on missing/malformed."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("Invalid %s=%r; falling back to default %s", name, raw, default)
        return default


@dataclasses.dataclass(frozen=True)
class WatchdogConfig:
    """
    Tunable thresholds for the CPU watchdog.

    Defaults err on the side of NOT firing during normal heavy work: a
    sample interval of 30 s, three consecutive samples over 80 percent
    own-process CPU, and a five-minute cooldown after a dump suppress
    double-fires during a sustained spin.
    """

    cpu_pct_threshold: float = _DEFAULT_CPU_PCT_THRESHOLD
    consecutive_samples: int = _DEFAULT_CONSECUTIVE_SAMPLES
    sample_interval_s: float = _DEFAULT_SAMPLE_INTERVAL_S
    cooldown_after_dump_s: float = _DEFAULT_COOLDOWN_AFTER_DUMP_S
    dump_retention: int = _DEFAULT_DUMP_RETENTION
    dump_dir: Path = _DEFAULT_DUMP_DIR

    @classmethod
    def from_env(cls) -> "WatchdogConfig":
        """
        Build a config from process environment.

        Honours ``SERENA_WEDGE_CPU_PCT``, ``SERENA_WEDGE_CONSECUTIVE_SAMPLES``,
        ``SERENA_WEDGE_SAMPLE_INTERVAL_S``, and ``SERENA_WEDGE_DUMP_RETENTION``.
        Missing or malformed values fall through to the dataclass defaults.
        The cooldown and dump_dir are not env-overrideable.
        """
        return cls(
            cpu_pct_threshold=_parse_float(_ENV_CPU_PCT, _DEFAULT_CPU_PCT_THRESHOLD),
            consecutive_samples=_parse_int(_ENV_CONSECUTIVE_SAMPLES, _DEFAULT_CONSECUTIVE_SAMPLES),
            sample_interval_s=_parse_float(_ENV_SAMPLE_INTERVAL_S, _DEFAULT_SAMPLE_INTERVAL_S),
            cooldown_after_dump_s=_DEFAULT_COOLDOWN_AFTER_DUMP_S,
            dump_retention=_parse_int(_ENV_DUMP_RETENTION, _DEFAULT_DUMP_RETENTION),
            dump_dir=_DEFAULT_DUMP_DIR,
        )


class _PsutilCpuSampler:
    """A :class:`CpuSampler` backed by :class:`psutil.Process`."""

    def __init__(self) -> None:
        # baseline: cpu_percent(interval=None) returns 0.0 on first call and
        # the delta since previous call thereafter. Prime once so the first
        # post-prime sample is meaningful.
        self._process = psutil.Process()
        self._process.cpu_percent(interval=None)

    def __call__(self) -> float:
        return self._process.cpu_percent(interval=None)


class _FaulthandlerTracebackDumper:
    """A :class:`TracebackDumper` that appends an all-thread traceback to ``path``."""

    def __call__(self, path: Path) -> None:
        with path.open("a") as fh:
            faulthandler.dump_traceback(file=fh, all_threads=True)


class WedgeWatchdog:
    """
    Daemon-thread CPU watchdog that dumps all-thread tracebacks on sustained spin.

    On :meth:`start`, spawns a daemon thread that samples own-process CPU on
    :attr:`WatchdogConfig.sample_interval_s` cadence. After
    :attr:`WatchdogConfig.consecutive_samples` consecutive samples at or
    above :attr:`WatchdogConfig.cpu_pct_threshold`, the watchdog writes a
    sidecar file under :attr:`WatchdogConfig.dump_dir`, prunes older sidecars
    to a retention window, and sleeps :attr:`WatchdogConfig.cooldown_after_dump_s`
    before re-arming. The thread is a daemon, so it never blocks process exit.

    The watchdog accepts pluggable strategies for CPU sampling, traceback
    dumping, and sleeping so unit tests can drive the loop without spawning
    a real high-CPU workload.
    """

    def __init__(
        self,
        config: WatchdogConfig | None = None,
        *,
        cpu_sampler: CpuSampler | None = None,
        traceback_dumper: TracebackDumper | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        # configuration and pluggable strategies (testability seam)
        self._config = config or WatchdogConfig.from_env()
        self._cpu_sampler: CpuSampler = cpu_sampler or _PsutilCpuSampler()
        self._traceback_dumper: TracebackDumper = traceback_dumper or _FaulthandlerTracebackDumper()
        self._sleeper: Callable[[float], None] = sleeper or time.sleep

        # internal loop state
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._consecutive_over_threshold = 0
        self._last_dump_path: Path | None = None
        self._dump_count = 0

    @property
    def config(self) -> WatchdogConfig:
        """The active watchdog configuration."""
        return self._config

    @property
    def last_dump_path(self) -> Path | None:
        """Path of the most recent sidecar dump this watchdog produced, if any."""
        return self._last_dump_path

    @property
    def dump_count(self) -> int:
        """The cumulative number of sidecar dumps this watchdog has produced."""
        return self._dump_count

    def is_running(self) -> bool:
        """Whether the watchdog daemon thread is currently alive."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """
        Spawn the watchdog daemon thread.

        Idempotent: a second call while already running is a no-op (a
        single watchdog per process is the invariant; multiple instances
        would race on the same dump directory).
        """
        if self.is_running():
            return
        # ensure the sidecar directory exists once at start-time so the loop
        # never has to handle ENOENT mid-dump.
        self._config.dump_dir.mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()
        self._consecutive_over_threshold = 0
        self._thread = threading.Thread(
            target=self._run,
            name="serena-wedge-watchdog",
            daemon=True,
        )
        log.info(
            "Starting wedge watchdog: cpu_pct>=%.1f for %d consecutive samples "
            "(every %.1fs); cooldown %.1fs; retention %d; dump_dir=%s",
            self._config.cpu_pct_threshold,
            self._config.consecutive_samples,
            self._config.sample_interval_s,
            self._config.cooldown_after_dump_s,
            self._config.dump_retention,
            self._config.dump_dir,
        )
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        """
        Signal the loop to exit and wait up to ``timeout`` seconds for the thread.

        Intended for tests; production daemons run until process exit and
        rely on the daemon-thread flag for cleanup.
        """
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run(self) -> None:
        """Main loop -- sample, count, dump on threshold, cooldown, repeat."""
        while not self._stop_event.is_set():
            # sample CPU and update the run-length count of over-threshold samples.
            try:
                pct = self._cpu_sampler()
            except Exception:
                log.exception("Wedge watchdog: cpu sample failed; pausing one interval")
                self._sleep_or_stop(self._config.sample_interval_s)
                continue

            if pct >= self._config.cpu_pct_threshold:
                self._consecutive_over_threshold += 1
            else:
                self._consecutive_over_threshold = 0

            # threshold-reached: write a sidecar dump, prune older ones, cool down,
            # then resume sampling. The run-length resets so the next streak
            # starts fresh.
            if self._consecutive_over_threshold >= self._config.consecutive_samples:
                try:
                    path = self._trigger_dump()
                    self._prune_old_dumps()
                    log.warning(
                        "Wedge watchdog: sustained CPU %.1f%% across %d samples; wrote %s",
                        pct,
                        self._consecutive_over_threshold,
                        path,
                    )
                except Exception:
                    log.exception("Wedge watchdog: failed to write or prune sidecar dump")
                self._consecutive_over_threshold = 0
                self._sleep_or_stop(self._config.cooldown_after_dump_s)
                continue

            self._sleep_or_stop(self._config.sample_interval_s)

    def _sleep_or_stop(self, seconds: float) -> None:
        """Sleep up to ``seconds`` while honouring an early :meth:`stop` request."""
        if seconds <= 0:
            return
        # use Event.wait so an in-progress stop() unblocks the loop immediately.
        self._stop_event.wait(seconds)

    def _trigger_dump(self) -> Path:
        """Write an all-thread traceback to a new ISO8601-stamped sidecar file."""
        timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self._config.dump_dir / f"wedge-{timestamp}.txt"
        self._traceback_dumper(path)
        self._last_dump_path = path
        self._dump_count += 1
        return path

    def _prune_old_dumps(self) -> None:
        """Keep only the ``dump_retention`` most recent ``wedge-*.txt`` files."""
        sidecars = sorted(
            self._config.dump_dir.glob(_DUMP_FILENAME_GLOB),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in sidecars[self._config.dump_retention :]:
            try:
                stale.unlink()
            except OSError:
                log.exception("Wedge watchdog: failed to prune %s", stale)


def start_wedge_watchdog() -> WedgeWatchdog:
    """
    Construct a watchdog from process environment and start it.

    Returns the watchdog so callers can keep a handle for tests or explicit
    shutdown. Production daemons can discard the return value; the thread
    is a daemon and runs until process exit.
    """
    watchdog = WedgeWatchdog()
    watchdog.start()
    return watchdog
