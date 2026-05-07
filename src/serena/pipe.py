"""Per-client stdio pipe forwarder for the Serena MCP daemon.

The pipe is a thin per-client process spawned by an MCP host (e.g. Claude Code)
that forwards JSON-RPC frames over stdio to a single shared Serena daemon
listening on a Unix-domain socket. It owns a stable ``session_id`` allocated by
the daemon at handshake time and tags every forwarded frame with that id so the
daemon can pin per-session state (active project, cursor manager, ...) to the
client's lifetime rather than to the upstream MCP transport's lifetime.

This module is the entry point added in T1 of
plan://Serena:serena/serena-pipe-implementation. T2 layers the handshake
protocol; T3 wires bidirectional frame forwarding; T4 fetches the tool catalog
from the daemon at startup. Until those land, :func:`run_pipe_client` only
validates that the CLI dispatched correctly and then refuses to run, so an
operator who attempts to use the transport before it is complete sees a clear
error rather than silent no-op.
"""

import logging

log = logging.getLogger(__name__)


class PipeNotImplementedError(NotImplementedError):
    """Raised when pipe-transport functionality is invoked before its task lands."""


def run_pipe_client(daemon_url: str) -> None:
    """Run the per-client pipe forwarder until the upstream stdio is closed.

    :param daemon_url: URI of the Serena daemon's listening socket. T1 accepts
        only the ``unix://`` scheme (e.g. ``unix:///tmp/serena-daemon.sock``);
        TCP and other schemes are reserved for future tasks.
    :raises PipeNotImplementedError: while T2-T4 are pending, since neither the
        handshake nor the frame-forwarding implementation has landed.
    """
    log.info("serena-pipe entry point invoked; daemon_url=%s", daemon_url)
    raise PipeNotImplementedError(
        "serena-pipe handshake (T2) and frame forwarding (T3) are not yet implemented; "
        "see plan://Serena:serena/serena-pipe-implementation"
    )
