"""Agent loop for the cursor-vs-RA pilot.

Spawns serena's MCP server as a stdio subprocess, connects an MCP client, and drives an
anthropic agent loop with prompt caching on the system prompt and tools array. The
spawned server is configured per-arm via a fixed_tools mode YAML so its auto-generated
system prompt naturally describes only that arm's tool surface (no cross-arm leakage).

Tool inputs/outputs and per-turn token usage are streamed to the trace writer.
Termination: agent emits ``DONE`` token (``end_turn``) → ``completed-with-done``;
otherwise stops at ``max_iters`` → ``hit-cap``; harness errors → ``error``.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from scripts.cursor_vs_ra.tool_surfaces import Arm
from scripts.cursor_vs_ra.trace import TokenUsage, TraceWriter

SERENA_REPO_ROOT = Path(__file__).resolve().parents[2]
"""resolves to the serena repo root (this file lives at scripts/cursor_vs_ra/agent.py)."""


@dataclass
class AgentConfig:
    """all knobs for a single agent run.

    The arm's context YAML controls the published tool surface server-side: it is
    loaded as a SerenaAgentContext and applied whole via ToolSet.apply, so its
    ``fixed_tools`` actually wipes the tool set (mode YAMLs do not — see
    ``configs/cursor_arm.yml`` for the underlying reason). The harness then
    asserts the published set matches ``arm.tool_names`` exactly before sending
    any request to the model. ``task`` and ``run_n`` flow through to the trace
    header.
    """

    arm: Arm
    context_yaml_path: Path
    project_path: Path
    model: str
    temperature: float
    max_iters: int
    max_tokens: int
    agent_prompt: str
    task: str
    run_n: int


@dataclass
class AgentResult:
    """run summary returned by the agent loop."""

    outcome: str
    total_turns: int
    total_usage: TokenUsage
    wall_time_s: float
    error: str | None = None


class CursorVsRAAgent:
    """drives one agentic conversation against serena, streaming events to a trace writer."""

    def __init__(self, config: AgentConfig, trace: TraceWriter) -> None:
        self._cfg = config
        self._trace = trace
        self._client = AsyncAnthropic()

    async def run(self) -> AgentResult:
        # build the serena MCP server command (stdio transport, arm's context applied)
        server_params = StdioServerParameters(
            command="uv",
            args=[
                "run",
                "--directory",
                str(SERENA_REPO_ROOT),
                "serena",
                "start-mcp-server",
                "--project",
                str(self._cfg.project_path),
                "--transport",
                "stdio",
                "--context",
                str(self._cfg.context_yaml_path),
                "--enable-web-dashboard",
                "false",
                "--enable-gui-log-window",
                "false",
            ],
        )

        # connect MCP client and verify the published tool set matches the arm's contract
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                init_result = await session.initialize()
                system_text = (init_result.instructions or "").strip()

                tools_response = await session.list_tools()
                published_names = {t.name for t in tools_response.tools}
                expected = self._cfg.arm.tool_names
                if published_names != expected:
                    raise RuntimeError(
                        f"MCP server tool set mismatch for arm '{self._cfg.arm.name}'.\n"
                        f"  expected: {sorted(expected)}\n"
                        f"  got:      {sorted(published_names)}\n"
                        f"  missing:  {sorted(expected - published_names)}\n"
                        f"  extra:    {sorted(published_names - expected)}"
                    )

                anthropic_tools = [_mcp_tool_to_anthropic(t) for t in tools_response.tools]
                # cache the static prefix (tools array) — last tool gets the cache_control marker
                if anthropic_tools:
                    anthropic_tools[-1] = {**anthropic_tools[-1], "cache_control": {"type": "ephemeral"}}

                system: list[dict[str, Any]] = [
                    {
                        "type": "text",
                        "text": system_text or "(serena did not provide initial instructions)",
                        "cache_control": {"type": "ephemeral"},
                    }
                ]

                messages: list[dict[str, Any]] = [
                    {"role": "user", "content": self._cfg.agent_prompt},
                ]

                self._trace.write_run_start(
                    arm=self._cfg.arm.name,
                    task=self._cfg.task,
                    run_n=self._cfg.run_n,
                    model=self._cfg.model,
                    temperature=self._cfg.temperature,
                    max_iters=self._cfg.max_iters,
                    tool_names=sorted(self._cfg.arm.tool_names),
                    agent_prompt=self._cfg.agent_prompt,
                )

                total_usage = TokenUsage()
                start_wall = time.monotonic()
                outcome = "hit-cap"
                error: str | None = None
                last_turn = 0

                try:
                    for turn in range(1, self._cfg.max_iters + 1):
                        last_turn = turn
                        # one anthropic API call per turn
                        turn_start = time.monotonic()
                        response = await self._client.messages.create(
                            model=self._cfg.model,
                            max_tokens=self._cfg.max_tokens,
                            temperature=self._cfg.temperature,
                            system=system,
                            tools=anthropic_tools,
                            messages=messages,
                        )
                        turn_latency_ms = int((time.monotonic() - turn_start) * 1000)
                        usage = TokenUsage.from_response(response.usage)
                        total_usage.add(usage)

                        # serialize content blocks for the trace and the next-turn replay
                        content_blocks = [_block_to_dict(b) for b in response.content]
                        self._trace.write_turn(
                            turn=turn,
                            content_blocks=content_blocks,
                            stop_reason=response.stop_reason or "",
                            usage=usage,
                            latency_ms=turn_latency_ms,
                        )
                        messages.append({"role": "assistant", "content": content_blocks})

                        # only end_turn with literal "DONE" counts as a clean completion
                        assistant_text = "".join(
                            getattr(b, "text", "") for b in response.content if b.type == "text"
                        )
                        if response.stop_reason == "end_turn":
                            outcome = "completed-with-done" if "DONE" in assistant_text else "ended-without-done"
                            break

                        if response.stop_reason != "tool_use":
                            outcome = f"unexpected-stop-{response.stop_reason}"
                            break

                        # dispatch every tool_use block to the MCP server, collect tool_results for the next user turn
                        tool_results: list[dict[str, Any]] = []
                        for block in response.content:
                            if block.type != "tool_use":
                                continue
                            tc_start = time.monotonic()
                            try:
                                tool_resp = await session.call_tool(block.name, dict(block.input))
                                tc_latency_ms = int((time.monotonic() - tc_start) * 1000)
                                result_text = _flatten_tool_content(tool_resp.content)
                                is_error = bool(tool_resp.isError)
                            except Exception as e:  # noqa: BLE001
                                tc_latency_ms = int((time.monotonic() - tc_start) * 1000)
                                result_text = f"[harness-side error] {type(e).__name__}: {e}"
                                is_error = True
                            self._trace.write_tool_call(
                                turn=turn,
                                tool_use_id=block.id,
                                tool_name=block.name,
                                tool_input=dict(block.input),
                                result_text=result_text,
                                is_error=is_error,
                                latency_ms=tc_latency_ms,
                            )
                            tool_results.append(
                                {
                                    "type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": result_text,
                                    "is_error": is_error,
                                }
                            )

                        messages.append({"role": "user", "content": tool_results})

                except Exception as e:  # noqa: BLE001
                    outcome = "error"
                    error = f"{type(e).__name__}: {e}"

                wall_time_s = time.monotonic() - start_wall
                self._trace.write_run_end(
                    outcome=outcome,
                    total_turns=last_turn,
                    total_usage=total_usage,
                    wall_time_s=wall_time_s,
                    error=error,
                )

                return AgentResult(
                    outcome=outcome,
                    total_turns=last_turn,
                    total_usage=total_usage,
                    wall_time_s=wall_time_s,
                    error=error,
                )


def _mcp_tool_to_anthropic(mcp_tool: Any) -> dict[str, Any]:
    """converts an MCP Tool object to the anthropic SDK tool schema shape."""
    return {
        "name": mcp_tool.name,
        "description": mcp_tool.description or "",
        "input_schema": mcp_tool.inputSchema,
    }


def _block_to_dict(block: Any) -> dict[str, Any]:
    """converts an anthropic content block to a JSON-serializable dict.

    Used both for trace logging and for the assistant message replay on the next turn.
    """
    return block.model_dump(mode="json", exclude_none=True)


def _flatten_tool_content(content_items: list[Any]) -> str:
    """joins MCP TextContent parts (and stringifies non-text content) into a single string."""
    parts: list[str] = []
    for item in content_items:
        if hasattr(item, "text"):
            parts.append(item.text)
        elif hasattr(item, "data"):
            parts.append(f"[non-text MCP content: {type(item).__name__}]")
        else:
            parts.append(str(item))
    return "\n".join(parts)


def extract_prompt_body(template_path: Path) -> str:
    """extracts the agent prompt content from the 4-backtick fenced block in the template."""
    text = template_path.read_text()
    match = re.search(r"````\s*\n(.*?)\n````", text, re.DOTALL)
    if match is None:
        raise ValueError(f"no 4-backtick fenced block found in {template_path}")
    return match.group(1).strip()
