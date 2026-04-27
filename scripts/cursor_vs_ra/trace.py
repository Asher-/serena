"""Run-trace artifacts for the cursor-vs-RA pilot.

Each run writes a JSONL event log (one record per agent turn or per tool call) and a
Markdown render of the same data. The JSONL is the canonical artifact for downstream
quantitative comparison; the Markdown is a reviewable view for the operator and the
blind judge.

Event types:
    run_start   - one per run, header metadata
    agent_turn  - one per anthropic API response (assistant message)
    tool_call   - one per tool_use the agent emitted, with the corresponding tool_result
    run_end     - one per run, terminal summary

Token usage on each agent_turn is the per-turn delta; ``run_end.total_usage`` sums them.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class TokenUsage:
    """tracks the four token counters returned by the anthropic API."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def add(self, other: "TokenUsage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens

    @classmethod
    def from_response(cls, usage: Any) -> "TokenUsage":
        """builds a TokenUsage from the ``response.usage`` field of an anthropic Message."""
        return cls(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        )


@dataclass
class TraceWriter:
    """streams JSONL trace events to a run directory and renders Markdown on demand."""

    run_dir: Path
    events: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._jsonl_path = self.run_dir / "trace.jsonl"
        # truncate any pre-existing trace from a prior aborted run
        self._jsonl_path.write_text("")

    @property
    def jsonl_path(self) -> Path:
        return self._jsonl_path

    def _emit(self, event: dict[str, Any]) -> None:
        # prepend timestamp and write immediately so a crash leaves a partial-but-valid log
        event = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **event}
        with self._jsonl_path.open("a") as f:
            f.write(json.dumps(event, default=str) + "\n")
        self.events.append(event)

    def write_run_start(
        self,
        *,
        arm: str,
        task: str,
        run_n: int,
        model: str,
        temperature: float,
        max_iters: int,
        tool_names: list[str],
        agent_prompt: str,
    ) -> None:
        self._emit(
            {
                "type": "run_start",
                "arm": arm,
                "task": task,
                "run_n": run_n,
                "model": model,
                "temperature": temperature,
                "max_iters": max_iters,
                "tool_names": tool_names,
                "agent_prompt_chars": len(agent_prompt),
            }
        )

    def write_turn(
        self,
        *,
        turn: int,
        content_blocks: list[dict[str, Any]],
        stop_reason: str,
        usage: TokenUsage,
        latency_ms: int,
    ) -> None:
        self._emit(
            {
                "type": "agent_turn",
                "turn": turn,
                "stop_reason": stop_reason,
                "usage": asdict(usage),
                "latency_ms": latency_ms,
                "content": content_blocks,
            }
        )

    def write_tool_call(
        self,
        *,
        turn: int,
        tool_use_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        result_text: str,
        is_error: bool,
        latency_ms: int,
    ) -> None:
        self._emit(
            {
                "type": "tool_call",
                "turn": turn,
                "tool_use_id": tool_use_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
                "result_text": result_text,
                "is_error": is_error,
                "latency_ms": latency_ms,
            }
        )

    def write_run_end(
        self,
        *,
        outcome: str,
        total_turns: int,
        total_usage: TokenUsage,
        wall_time_s: float,
        error: str | None = None,
    ) -> None:
        self._emit(
            {
                "type": "run_end",
                "outcome": outcome,
                "total_turns": total_turns,
                "total_usage": asdict(total_usage),
                "wall_time_s": wall_time_s,
                "error": error,
            }
        )

    def render_markdown(self) -> Path:
        """renders the JSONL events to a human-readable trace.md alongside trace.jsonl."""
        md_path = self.run_dir / "trace.md"
        md_path.write_text(MarkdownRenderer(self.events).render())
        return md_path


class MarkdownRenderer:
    """renders a sequence of trace events as a single Markdown document."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    def render(self) -> str:
        # bucket events so the header can be rendered before the per-turn body
        run_start = next((e for e in self._events if e["type"] == "run_start"), None)
        run_end = next((e for e in self._events if e["type"] == "run_end"), None)
        turns = [e for e in self._events if e["type"] == "agent_turn"]
        tool_calls_by_turn: dict[int, list[dict[str, Any]]] = {}
        for tc in (e for e in self._events if e["type"] == "tool_call"):
            tool_calls_by_turn.setdefault(tc["turn"], []).append(tc)

        lines: list[str] = []
        # header section: run identity, model knobs, tool surface, outcome (if known)
        if run_start is not None:
            lines.append(f"# {run_start['arm']} arm — {run_start['task']} — run {run_start['run_n']}")
            lines.append("")
            lines.append(f"- Model: `{run_start['model']}`")
            lines.append(f"- Temperature: {run_start['temperature']}")
            lines.append(f"- Max iters: {run_start['max_iters']}")
            tool_list = ", ".join(f"`{t}`" for t in run_start["tool_names"])
            lines.append(f"- Tools ({len(run_start['tool_names'])}): {tool_list}")
            lines.append(f"- Agent prompt chars: {run_start['agent_prompt_chars']}")
            lines.append(f"- Started: {run_start['ts']}")
        if run_end is not None:
            lines.append(f"- Outcome: **{run_end['outcome']}**")
            lines.append(f"- Total turns: {run_end['total_turns']}")
            lines.append(f"- Wall time: {run_end['wall_time_s']:.1f}s")
            usage = run_end["total_usage"]
            lines.append(
                f"- Tokens (cumulative): in={usage['input_tokens']}, out={usage['output_tokens']}, "
                f"cache_create={usage['cache_creation_input_tokens']}, "
                f"cache_read={usage['cache_read_input_tokens']}"
            )
            if run_end.get("error"):
                lines.append(f"- Error: `{run_end['error']}`")
        lines.append("")
        lines.append("---")
        lines.append("")

        # body: one section per agent turn, with tool_use+result pairs as collapsibles
        for turn in turns:
            lines.append(
                f"## Turn {turn['turn']} — `{turn['stop_reason']}` ({turn['latency_ms']}ms)"
            )
            usage = turn["usage"]
            lines.append(
                f"_tokens: in={usage['input_tokens']}, out={usage['output_tokens']}, "
                f"cache_create={usage['cache_creation_input_tokens']}, "
                f"cache_read={usage['cache_read_input_tokens']}_"
            )
            lines.append("")
            for block in turn["content"]:
                btype = block.get("type")
                if btype == "text":
                    lines.append(block.get("text", ""))
                    lines.append("")
                elif btype == "tool_use":
                    name = block.get("name", "?")
                    input_json = json.dumps(block.get("input", {}), indent=2, default=str)
                    matching = next(
                        (
                            tc
                            for tc in tool_calls_by_turn.get(turn["turn"], [])
                            if tc["tool_use_id"] == block.get("id")
                        ),
                        None,
                    )
                    summary_suffix = ""
                    if matching is not None:
                        err_marker = " ⚠️" if matching["is_error"] else ""
                        summary_suffix = f" — {matching['latency_ms']}ms{err_marker}"
                    lines.append(
                        f"<details><summary>tool_use: <code>{name}</code>{summary_suffix}</summary>"
                    )
                    lines.append("")
                    lines.append("```json")
                    lines.append(input_json)
                    lines.append("```")
                    if matching is not None:
                        prefix = "**ERROR** " if matching["is_error"] else ""
                        lines.append("")
                        lines.append(f"{prefix}**Result:**")
                        lines.append("")
                        lines.append("```")
                        lines.append(matching["result_text"])
                        lines.append("```")
                    lines.append("")
                    lines.append("</details>")
                    lines.append("")
                else:
                    # unknown block type (image, etc.) — render as raw json
                    lines.append("```json")
                    lines.append(json.dumps(block, indent=2, default=str))
                    lines.append("```")
                    lines.append("")
        return "\n".join(lines)
