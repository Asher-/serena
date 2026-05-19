"""Tests for the streamable-http session-key derivation in :meth:`Tool.apply_ex`.

This module locks in the three-tier session-key contract from
plan://Serena:serena/streamable-http-cc-session-id-pass-through-v2-impl (task t20,
fka t8). The tiers, highest precedence first:

  1. ``_PIPE_SESSION_ID_VAR`` -- the pipe transport supplies the project_root
     (per plan://Serena:serena/serena-pipe-session-id-is-the-session-id).
  2. ``X-Forwarded-Mcp-Session-Id`` header -- the multiplexer forwards the
     inbound CC client's Mcp-Session-Id verbatim. apply_ex probes both
     ``mcp_ctx.request_context.request.headers`` and ``mcp_ctx.request.headers``
     so it works across FastMCP transport variants. The value is used as a
     **string** session_key; a finalizer is registered on ``mcp_ctx.session``.
  3. ``id(mcp_ctx.session)`` -- legacy direct-stdio / streamable-http without
     the multiplexer. A one-time per-session warning is emitted via
     :meth:`SerenaAgent._warn_missing_forwarded_session_id_once`.

The tests exercise these tiers without spinning up an MCP server, language
server, or real Tool subclass: a tiny in-test Tool stub marked
:class:`ToolMarkerDoesNotRequireActiveProject` is instantiated on a minimal
:class:`SerenaAgent` and ``apply_ex`` is called with mock ``mcp_ctx``
objects whose ``headers`` mapping is controlled per case. apply_ex always
performs the session-key derivation **before** dispatching to the task
executor, so the side effects on the agent's per-session state are visible
synchronously on return regardless of whether the inner task succeeds.
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest

from serena.agent import _PIPE_SESSION_ID_VAR, SerenaAgent
from serena.config.serena_config import SerenaConfig
from serena.tools.tools_base import Tool, ToolMarkerDoesNotRequireActiveProject


# --- test infrastructure --------------------------------------------------


class _NoOpProbeTool(Tool, ToolMarkerDoesNotRequireActiveProject):
    """A Tool subclass that never needs a project and always returns OK.

    The body of ``apply_ex`` only runs its session-key derivation before the
    task executor is engaged; the actual ``apply`` may or may not run
    depending on whether ``is_active()`` returns True. Either way, the
    side effects on :attr:`SerenaAgent._active_projects_by_session`,
    :attr:`SerenaAgent._session_finalizers`, and
    :attr:`SerenaAgent._warned_missing_forwarded_session_keys` happen
    synchronously and are observable when apply_ex returns.
    """

    def apply(self) -> str:  # pragma: no cover -- not reached when is_active is patched False
        return "OK"


class _FakeSession:
    """Minimal stand-in for the FastMCP session object that ``id()`` and
    :func:`weakref.finalize` both accept.

    Using a real Python class (rather than ``object()``) keeps
    :func:`weakref.finalize` from raising ``TypeError`` -- ``object`` does
    not support weak references, but a user-defined class does.
    """

    def __init__(self) -> None:
        self.client_params = None


class _CaseInsensitiveHeaders(dict[str, str]):
    """Starlette-style header map: ``get`` matches keys case-insensitively.

    FastMCP exposes either ``Starlette.Headers`` (truly case-insensitive) or
    a plain dict that the SDK populated already lower-cased. The header read
    logic in :meth:`Tool.apply_ex` calls ``.get(...)`` with both the
    lower-cased and canonical-cased names; this stub honours both lookups.
    """

    def get(self, key: str, default: Any = None) -> Any:
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


def _make_mcp_ctx(
    *,
    forwarded_header: str | None = None,
    header_attr_path: str = "request_context.request.headers",
    session: _FakeSession | None = None,
) -> MagicMock:
    """Build a mock ``mcp_ctx`` whose header attribute path is one of the
    FastMCP variants Tool.apply_ex probes.

    :param forwarded_header: value of ``X-Forwarded-Mcp-Session-Id`` if set;
        ``None`` means the header is absent (no key in the headers map).
    :param header_attr_path: which attribute chain to populate. Either
        ``"request_context.request.headers"`` (FastMCP HTTP variant) or
        ``"request.headers"`` (alternate variant). Forces tests to cover both.
    :param session: a :class:`_FakeSession` that becomes ``mcp_ctx.session``
        so :func:`id` and weakref.finalize both work against it.
    """
    ctx = MagicMock()
    ctx.session = session if session is not None else _FakeSession()
    headers = _CaseInsensitiveHeaders()
    if forwarded_header is not None:
        headers["x-forwarded-mcp-session-id"] = forwarded_header

    if header_attr_path == "request_context.request.headers":
        ctx.request_context.request.headers = headers
        # ensure the alternate path doesn't accidentally also have the header
        ctx.request = MagicMock()
        ctx.request.headers = _CaseInsensitiveHeaders()
    elif header_attr_path == "request.headers":
        ctx.request_context = None  # disable the request_context probe path
        ctx.request.headers = headers
    elif header_attr_path == "neither":
        ctx.request_context = None
        ctx.request = None
    else:
        raise ValueError(f"unknown header_attr_path: {header_attr_path}")
    return ctx


@pytest.fixture
def agent() -> Iterator[SerenaAgent]:
    """Build a minimal :class:`SerenaAgent` with no active project."""
    config = SerenaConfig(gui_log_window=False, web_dashboard=False)
    a = SerenaAgent(serena_config=config)
    yield a
    a.on_shutdown(timeout=0.5)


@pytest.fixture
def tool(agent: SerenaAgent, monkeypatch: pytest.MonkeyPatch) -> _NoOpProbeTool:
    """Instantiate the probe tool on the agent. ``is_active`` is patched to
    return True so the task closure runs through to return ``"OK"`` rather
    than the not-active error path; both branches exercise the session-key
    derivation we care about, but the green path makes assertions cleaner.
    """
    t = _NoOpProbeTool(agent)
    monkeypatch.setattr(t, "is_active", lambda: True)
    # record_tool_usage runs on the green path; stub it so the test agent's
    # tool-usage-stats backend isn't required for these unit tests.
    monkeypatch.setattr(agent, "record_tool_usage", lambda *a, **kw: None)
    return t


def _reset_pipe_var() -> contextvars.Token[str | None]:
    """Clear ``_PIPE_SESSION_ID_VAR`` for the duration of a test; restore on
    fixture teardown.
    """
    return _PIPE_SESSION_ID_VAR.set(None)


# --- tier 2: X-Forwarded-Mcp-Session-Id header present -------------------


class TestForwardedHeaderTier:
    """Header-tier behaviour (tier 2): apply_ex must key on the inbound CC
    session id when the multiplexer forwards it.
    """

    def test_header_via_request_context_path_yields_str_session_key(
        self, agent: SerenaAgent, tool: _NoOpProbeTool
    ) -> None:
        """Case (a)+(e): X-Forwarded-Mcp-Session-Id present via
        ``mcp_ctx.request_context.request.headers`` -> session_key equals
        the header value AND is a ``str`` (NOT an ``int`` like the
        ``id()``-derived legacy key).
        """
        token = _reset_pipe_var()
        try:
            cc_session_id = "cc-session-abc-via-request-context"
            ctx = _make_mcp_ctx(
                forwarded_header=cc_session_id,
                header_attr_path="request_context.request.headers",
            )
            tool.apply_ex(mcp_ctx=ctx, log_call=False)

            # the finalizer is registered under the header value as a str key
            assert cc_session_id in agent._session_finalizers, (
                f"finalizer not registered under str key {cc_session_id!r}; "
                f"keys present: {list(agent._session_finalizers.keys())!r}"
            )
            assert isinstance(cc_session_id, str)
            # the int-keyed legacy slot must NOT have been touched
            assert not any(isinstance(k, int) for k in agent._session_finalizers), (
                f"unexpected int-keyed finalizer registered alongside str key: "
                f"{list(agent._session_finalizers.keys())!r}"
            )
            # no missing-header warning was emitted
            assert agent._warned_missing_forwarded_session_keys == set()
        finally:
            _PIPE_SESSION_ID_VAR.reset(token)

    def test_header_via_request_path_yields_str_session_key(
        self, agent: SerenaAgent, tool: _NoOpProbeTool
    ) -> None:
        """Case (a)+(e) alternate attribute path: header reached via
        ``mcp_ctx.request.headers`` when ``request_context`` is unavailable.
        This path is the second probe in apply_ex's header-extraction loop;
        the test confirms the fallback works.
        """
        token = _reset_pipe_var()
        try:
            cc_session_id = "cc-session-xyz-via-request-attr"
            ctx = _make_mcp_ctx(
                forwarded_header=cc_session_id,
                header_attr_path="request.headers",
            )
            tool.apply_ex(mcp_ctx=ctx, log_call=False)
            assert cc_session_id in agent._session_finalizers, (
                "fallback header probe via mcp_ctx.request.headers must yield a registered finalizer; "
                f"keys: {list(agent._session_finalizers.keys())!r}"
            )
            assert agent._warned_missing_forwarded_session_keys == set()
        finally:
            _PIPE_SESSION_ID_VAR.reset(token)

    def test_two_distinct_headers_on_same_session_register_distinct_finalizers(
        self, agent: SerenaAgent, tool: _NoOpProbeTool
    ) -> None:
        """Case (c): the multiplexer multiplexes many CC sessions onto a
        single persistent ``mcp_ctx.session``. apply_ex must register a
        distinct finalizer for each X-Forwarded value seen on the same
        underlying session object.
        """
        token = _reset_pipe_var()
        try:
            shared_session = _FakeSession()
            cc_a = "cc-session-A-multiplexed"
            cc_b = "cc-session-B-multiplexed"

            ctx_a = _make_mcp_ctx(forwarded_header=cc_a, session=shared_session)
            ctx_b = _make_mcp_ctx(forwarded_header=cc_b, session=shared_session)

            tool.apply_ex(mcp_ctx=ctx_a, log_call=False)
            tool.apply_ex(mcp_ctx=ctx_b, log_call=False)

            # both string keys are registered, and they are independent
            assert cc_a in agent._session_finalizers
            assert cc_b in agent._session_finalizers
            assert agent._session_finalizers[cc_a] is not agent._session_finalizers[cc_b], (
                "each forwarded CC session id must own its own finalizer object even when "
                "they share the underlying mcp_ctx.session target"
            )
        finally:
            _PIPE_SESSION_ID_VAR.reset(token)


# --- tier 3: header absent, fall back to id(mcp_ctx.session) -------------


class TestLegacyIdFallbackTier:
    """Tier-3 behaviour: legacy clients that don't go through the multiplexer
    (no ``X-Forwarded-Mcp-Session-Id``) must fall back to
    ``id(mcp_ctx.session)`` AND emit a single warning per session.
    """

    def test_header_absent_uses_id_key_and_logs_single_warning(
        self, agent: SerenaAgent, tool: _NoOpProbeTool, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Case (b): no header -> session_key == id(mcp_ctx.session), and a
        single warning is emitted. Repeated calls on the same session MUST
        NOT emit duplicate warnings; the agent's warned-key tracker
        deduplicates by ``session_key``.
        """
        token = _reset_pipe_var()
        try:
            shared_session = _FakeSession()
            ctx = _make_mcp_ctx(forwarded_header=None, session=shared_session)
            expected_key = id(shared_session)

            with caplog.at_level(logging.WARNING, logger="serena.agent"):
                tool.apply_ex(mcp_ctx=ctx, log_call=False)
                tool.apply_ex(mcp_ctx=ctx, log_call=False)
                tool.apply_ex(mcp_ctx=ctx, log_call=False)

            # session_key was the id(), tracked in the warning set
            assert expected_key in agent._warned_missing_forwarded_session_keys, (
                f"expected id({shared_session!r})={expected_key} in warned-key set; "
                f"got {agent._warned_missing_forwarded_session_keys!r}"
            )
            assert expected_key in agent._session_finalizers, (
                "the id() session_key must have a finalizer registered just like any other tier"
            )

            # exactly ONE warning emitted across the three calls
            warning_msgs = [
                r.getMessage()
                for r in caplog.records
                if r.levelno == logging.WARNING
                and "X-Forwarded-Mcp-Session-Id" in r.getMessage()
            ]
            assert len(warning_msgs) == 1, (
                f"expected exactly one missing-header warning across 3 apply_ex calls on the same session; "
                f"got {len(warning_msgs)}: {warning_msgs!r}"
            )
        finally:
            _PIPE_SESSION_ID_VAR.reset(token)


# --- tier 1: pipe transport wins over header -----------------------------


class TestPipeSessionIdPrecedence:
    """Tier-1 behaviour: ``_PIPE_SESSION_ID_VAR`` is the highest-precedence
    source of ``session_key``. When set, the X-Forwarded header MUST be
    ignored and no missing-header warning may be emitted (pipe is its own
    valid tier, not the legacy fallback).
    """

    def test_pipe_var_set_ignores_forwarded_header_and_skips_finalizer(
        self, agent: SerenaAgent, tool: _NoOpProbeTool
    ) -> None:
        """Case (d): pipe wins. Even if the multiplexer happens to also set
        the X-Forwarded header on a request whose context already carries a
        pipe-asserted project_root, apply_ex must use the pipe value as
        session_key and skip the mcp_ctx finalizer registration (pipe
        eviction is driven by socket disconnect, not GC).
        """
        pipe_project_root = "/tmp/test-project-root-as-session-id"
        header_value = "cc-session-should-be-ignored"
        ctx = _make_mcp_ctx(forwarded_header=header_value)

        token = _PIPE_SESSION_ID_VAR.set(pipe_project_root)
        try:
            tool.apply_ex(mcp_ctx=ctx, log_call=False)

            # neither the header value nor the id() ended up as a session_key:
            # the pipe value won, and the pipe tier does NOT register an mcp_ctx
            # finalizer (eviction is socket-disconnect driven for the pipe path).
            assert header_value not in agent._session_finalizers, (
                f"header value should be ignored when pipe var is set; "
                f"got finalizers keyed at: {list(agent._session_finalizers.keys())!r}"
            )
            assert id(ctx.session) not in agent._session_finalizers, (
                "pipe tier must NOT fall through to id(mcp_ctx.session) finalizer registration"
            )
            # and no missing-header warning was emitted -- the pipe is valid tier 1, not legacy
            assert agent._warned_missing_forwarded_session_keys == set()
        finally:
            _PIPE_SESSION_ID_VAR.reset(token)
