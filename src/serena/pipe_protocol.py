"""Wire protocol shared by the per-client pipe forwarder and the Serena daemon.

The pipe and the daemon communicate over a Unix-domain socket using
newline-delimited JSON envelopes. Each envelope has two channels:

* ``meta`` -- pipe-protocol metadata (the daemon-asserted ``session_id`` once the
  handshake is complete; reserved for future control-plane fields).
* ``frame`` -- the JSON-RPC payload the daemon forwards to FastMCP, or the
  daemon's response delivered back to the pipe.

The handshake (T2) is carried as a JSON-RPC method ``mcp/session/open`` in the
``frame`` channel; the daemon recognises it as a control-plane message,
allocates a fresh UUID4 ``session_id`` per pipe connection, and returns it in
the ``meta`` channel of the response. From that response onward the pipe
stamps every forwarded frame with ``meta.session_id`` so the daemon can pin
per-session state to the pipe's lifetime rather than to the upstream MCP
transport's lifetime.

The catalog fetch (T4) is carried as a JSON-RPC method ``pipe/catalog/get``
in the ``frame`` channel; like the handshake it never reaches FastMCP. The
daemon answers locally from its authoritative tool catalog. The pipe issues
exactly one ``pipe/catalog/get`` immediately after the handshake completes
and uses the returned ``tools`` list to answer every upstream ``tools/list``
without round-tripping to the daemon.
"""

import json
from dataclasses import dataclass, field
from typing import Any, ClassVar


class PipeProtocolError(Exception):
    """Raised when an envelope on the pipe-to-daemon socket violates the contract."""


@dataclass(frozen=True)
class PipeEnvelope:
    """One newline-delimited JSON message on the pipe-to-daemon socket.

    :ivar meta: Pipe-protocol metadata. The daemon-asserted ``session_id`` lives
        here from the handshake response onward; absent on the initial request.
    :ivar frame: The JSON-RPC payload the envelope wraps. Empty dict is
        permitted for future control-plane messages that don't need a payload.
    """

    meta: dict[str, Any] = field(default_factory=dict)
    frame: dict[str, Any] = field(default_factory=dict)

    def to_bytes(self) -> bytes:
        r"""Encode the envelope as a UTF-8 JSON line terminated by ``\n``."""
        return (json.dumps({"meta": self.meta, "frame": self.frame}) + "\n").encode("utf-8")

    @classmethod
    def from_bytes(cls, line: bytes) -> "PipeEnvelope":
        """Decode one JSON line back into an envelope.

        :param line: One JSON line as taken off the wire. A trailing newline is
            stripped if present, so byte buffers from either ``readuntil`` or
            length-stripped reads are both accepted.
        :raises PipeProtocolError: if ``line`` is not valid JSON or does not
            match the envelope shape (``{"meta": {...}, "frame": {...}}``).
        """
        # decode the bytes-on-wire to an in-memory dict, surfacing any malformed
        # JSON as a typed PipeProtocolError so callers don't have to catch a
        # bare JSONDecodeError
        text = line.decode("utf-8").rstrip("\n")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PipeProtocolError(f"malformed envelope JSON: {exc}") from exc

        # validate the envelope shape; reject anything that isn't the documented
        # two-channel structure so contract violations surface loudly rather
        # than silently degrading downstream
        if not isinstance(payload, dict) or "meta" not in payload or "frame" not in payload:
            raise PipeProtocolError(f"envelope must contain 'meta' and 'frame' keys; got {payload!r}")
        meta = payload["meta"]
        frame = payload["frame"]
        if not isinstance(meta, dict) or not isinstance(frame, dict):
            raise PipeProtocolError(f"envelope channels must be JSON objects; got meta={type(meta).__name__} frame={type(frame).__name__}")

        return cls(meta=meta, frame=frame)


class PipeHandshake:
    """Encoder/decoder for the ``mcp/session/open`` handshake exchange.

    The handshake is the first envelope a pipe sends after connecting and the
    daemon's response is the only way the pipe learns its assigned
    ``session_id``. The four classmethods below are the canonical entry points
    for encoding and decoding handshake messages; direct manipulation of the
    underlying envelope is reserved for protocol-level tests.
    """

    METHOD: ClassVar[str] = "mcp/session/open"
    """JSON-RPC method name reserved for the pipe handshake."""

    REQUEST_ID: ClassVar[int] = 1
    """Fixed JSON-RPC id for the handshake request; the pipe sends exactly one."""

    @classmethod
    def request(cls, project_root: str) -> PipeEnvelope:
        """Build the pipe-side handshake envelope to send on connect.

        :param project_root: Absolute path of the project the pipe-client is
            operating on. Stamped into ``meta.session_id`` so the daemon keys
            per-session state on it. The project root IS the session identity:
            two pipe-clients on the same project root are one logical session
            by design and share daemon-side per-session state.
        """
        return PipeEnvelope(
            meta={"session_id": project_root},
            frame={"jsonrpc": "2.0", "method": cls.METHOD, "id": cls.REQUEST_ID},
        )

    @classmethod
    def response(cls, session_id: str) -> PipeEnvelope:
        """Build the daemon-side handshake response carrying ``session_id``.

        :param session_id: The UUID4 the daemon allocated for the connecting
            pipe. Lives in the envelope's ``meta`` channel where every
            subsequent frame's ``session_id`` will live.
        """
        return PipeEnvelope(
            meta={"session_id": session_id},
            frame={"jsonrpc": "2.0", "id": cls.REQUEST_ID, "result": {"ok": True}},
        )

    @classmethod
    def is_request(cls, envelope: PipeEnvelope) -> bool:
        """Return ``True`` iff ``envelope`` is a well-formed handshake request."""
        return envelope.frame.get("method") == cls.METHOD and envelope.frame.get("jsonrpc") == "2.0"

    @classmethod
    def session_id_from_response(cls, envelope: PipeEnvelope) -> str:
        """Extract the daemon-asserted ``session_id`` from a handshake response.

        :param envelope: The response envelope as decoded by
            :meth:`PipeEnvelope.from_bytes`.
        :raises PipeProtocolError: if the envelope is missing
            ``meta.session_id`` or carries it as anything other than a non-empty
            string.
        """
        session_id = envelope.meta.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise PipeProtocolError(f"handshake response missing meta.session_id; got meta={envelope.meta!r}")
        return session_id


class PipeCatalog:
    """Encoder/decoder for the ``pipe/catalog/get`` exchange the pipe issues after handshake.

    The catalog fetch is the second control-plane exchange on the pipe, run
    once per connection immediately after :class:`PipeHandshake` completes.
    The daemon answers locally with its authoritative tool catalog; FastMCP
    never sees the request. The pipe caches the returned list for the
    connection's lifetime and uses it to answer every upstream ``tools/list``
    locally, so the catalog stays stable even if the daemon restarts mid-
    session (the next pipe connection re-fetches).
    """

    METHOD: ClassVar[str] = "pipe/catalog/get"
    """JSON-RPC method name reserved for the pipe catalog fetch."""

    REQUEST_ID: ClassVar[int] = 2
    """Fixed JSON-RPC id for the catalog request; the pipe sends exactly one per connection."""

    @classmethod
    def request(cls) -> PipeEnvelope:
        """Build the pipe-side catalog request envelope to send after handshake.

        The envelope's ``meta`` channel is empty: stamping ``session_id`` is the
        caller's responsibility, since the request is sent post-handshake when
        the session_id is already known and every post-handshake envelope
        carries it.
        """
        return PipeEnvelope(
            meta={},
            frame={"jsonrpc": "2.0", "method": cls.METHOD, "id": cls.REQUEST_ID},
        )

    @classmethod
    def response(cls, session_id: str, tools: list[dict[str, Any]]) -> PipeEnvelope:
        """Build the daemon-side catalog response carrying the tool list.

        :param session_id: The daemon-allocated session_id for the connection
            that issued the request. Stamped on the response so the pipe can
            assert symmetry with the handshake response.
        :param tools: Authoritative list of tool definitions, each as a JSON-
            serializable dict (``name``, ``description``, ``inputSchema``,
            etc.). The daemon's :class:`CatalogProvider` produces this list.
        """
        return PipeEnvelope(
            meta={"session_id": session_id},
            frame={"jsonrpc": "2.0", "id": cls.REQUEST_ID, "result": {"tools": tools}},
        )

    @classmethod
    def is_request(cls, envelope: PipeEnvelope) -> bool:
        """Return ``True`` iff ``envelope`` is a well-formed catalog request."""
        return envelope.frame.get("method") == cls.METHOD and envelope.frame.get("jsonrpc") == "2.0"

    @classmethod
    def tools_from_response(cls, envelope: PipeEnvelope) -> list[dict[str, Any]]:
        """Extract the tool list from a daemon-side catalog response.

        :param envelope: The response envelope as decoded by
            :meth:`PipeEnvelope.from_bytes`.
        :raises PipeProtocolError: if the envelope is missing
            ``frame.result.tools`` or carries either field as the wrong type.
        """
        # validate the result channel; a missing or non-object result means the
        # daemon answered with something other than the documented catalog
        # response shape, which we reject loudly rather than coerce
        result = envelope.frame.get("result")
        if not isinstance(result, dict):
            raise PipeProtocolError(f"catalog response missing frame.result object; got frame={envelope.frame!r}")

        # validate the tools list; the daemon must always return a list, even
        # if empty, so a missing or non-list value is a contract violation
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise PipeProtocolError(f"catalog response missing frame.result.tools list; got result={result!r}")
        return tools
