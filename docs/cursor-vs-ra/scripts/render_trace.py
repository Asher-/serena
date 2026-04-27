#!/usr/bin/env python3
"""Render a Claude Code session JSONL into a readable markdown trace.

Used as input to the post-pilot judges:
  - Phase (b) Reviewer Nav Q&A: trace-as-input, judge-answers-questions
  - Phase (c) Blind A/B: two traces side-by-side with --redact

JSONL schema (Claude Code session log):
  type=user        : prompt + tool_result blocks (in .message.content[])
  type=assistant   : text + tool_use blocks (in .message.content[])
  type=last-prompt, queue-operation : metadata; ignored
  type=attachment  : pasted/binary content; rendered as a stub line

Lines without a recognized "type" are skipped. Content elements with an
unrecognized "type" are rendered as a stub line.

--redact replaces every distinct tool name with a per-trace consistent
placeholder (tool_a, tool_b, ...). The mapping is alphabetical over the
sorted set of tool names that actually appeared in the trace, so the
mapping is deterministic and reproducible. Tool input keys are NOT
redacted; the judge prompt warns about residual artifacts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_jsonl(path):
    out = []
    with path.open() as f:
        for i, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                sys.stderr.write("warn: line " + str(i) + " parse failed: " + str(e) + "\n")
    return out


def collect_tool_names(events):
    seen = set()
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        msg = ev.get("message") or {}
        for block in msg.get("content") or []:
            if block.get("type") == "tool_use":
                name = block.get("name")
                if isinstance(name, str):
                    seen.add(name)
    return sorted(seen)


def build_redaction(tool_names):
    mapping = {}
    for i, name in enumerate(tool_names):
        suffix = ""
        n = i
        while True:
            suffix = chr(ord("a") + n % 26) + suffix
            n = n // 26 - 1
            if n < 0:
                break
        mapping[name] = "tool_" + suffix
    return mapping


def truncate(text, cap):
    if cap <= 0 or len(text) <= cap:
        return text
    return text[:cap] + "\n\n... [truncated; " + str(len(text) - cap) + " chars elided] ..."


def render_tool_input(input_obj, cap):
    if input_obj is None:
        return ""
    text = json.dumps(input_obj, indent=2, ensure_ascii=False)
    return truncate(text, cap)


def render_tool_result(content, cap):
    if isinstance(content, str):
        return truncate(content, cap)
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                else:
                    parts.append(json.dumps(block, ensure_ascii=False))
            else:
                parts.append(str(block))
        return truncate("\n".join(parts), cap)
    return truncate(json.dumps(content, ensure_ascii=False), cap)


def render(events, redact, cap):
    tool_names = collect_tool_names(events)
    redaction = build_redaction(tool_names) if redact else {}

    out = []
    out.append("# Investigation trace")
    out.append("")
    if redact:
        out.append("Tool names redacted; " + str(len(redaction)) + " distinct tool(s) appear in this trace.")
    else:
        out.append(str(len(tool_names)) + " distinct tool(s) appear: " + (", ".join(tool_names) if tool_names else "(none)"))
    out.append("")
    out.append("---")
    out.append("")

    turn = 0
    for ev in events:
        t = ev.get("type")
        if t in ("last-prompt", "queue-operation"):
            continue

        if t == "attachment":
            mime = ev.get("mime_type", "?")
            out.append("_[attachment: " + mime + "]_")
            out.append("")
            continue

        if t == "user":
            msg = ev.get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                turn += 1
                out.append("## Turn " + str(turn) + " -- User prompt")
                out.append("")
                out.append(content.rstrip())
                out.append("")
                continue
            if isinstance(content, list):
                for block in content:
                    btype = block.get("type") if isinstance(block, dict) else None
                    if btype == "text":
                        turn += 1
                        out.append("## Turn " + str(turn) + " -- User prompt")
                        out.append("")
                        out.append(block.get("text", "").rstrip())
                        out.append("")
                    elif btype == "tool_result":
                        tool_id = block.get("tool_use_id", "?")
                        is_error = block.get("is_error", False)
                        marker = " [ERROR]" if is_error else ""
                        out.append("### Tool result" + marker + " (id=" + tool_id + ")")
                        out.append("")
                        out.append("```")
                        out.append(render_tool_result(block.get("content"), cap))
                        out.append("```")
                        out.append("")
                    else:
                        out.append("_[unknown user content block: type=" + str(btype) + "]_")
                        out.append("")
            continue

        if t == "assistant":
            msg = ev.get("message") or {}
            content = msg.get("content") or []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "").rstrip()
                    if text:
                        out.append("### Assistant")
                        out.append("")
                        out.append(text)
                        out.append("")
                elif btype == "tool_use":
                    name = block.get("name", "?")
                    display = redaction.get(name, name)
                    tool_id = block.get("id", "?")
                    out.append("### Tool call: " + display + " (id=" + tool_id + ")")
                    out.append("")
                    out.append("```json")
                    out.append(render_tool_input(block.get("input"), cap))
                    out.append("```")
                    out.append("")
                elif btype == "thinking":
                    out.append("### Assistant (thinking)")
                    out.append("")
                    out.append(block.get("thinking", "").rstrip())
                    out.append("")
                else:
                    out.append("_[unknown assistant content block: type=" + str(btype) + "]_")
                    out.append("")
            continue

        out.append("_[unknown event type: " + str(t) + "]_")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("jsonl", type=Path, help="Path to the session JSONL file")
    ap.add_argument("--redact", action="store_true",
                    help="Replace tool names with tool_a, tool_b, ... per-trace consistent placeholders")
    ap.add_argument("--max-result-chars", type=int, default=0,
                    help="Truncate any tool result or input larger than N chars (0 = no cap)")
    ap.add_argument("-o", "--output", type=Path, default=None,
                    help="Output path (default: stdout)")
    args = ap.parse_args()

    if not args.jsonl.exists():
        sys.stderr.write("error: " + str(args.jsonl) + " does not exist\n")
        return 2

    events = load_jsonl(args.jsonl)
    md = render(events, redact=args.redact, cap=args.max_result_chars)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(md)
    else:
        sys.stdout.write(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
