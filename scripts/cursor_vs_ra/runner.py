"""CLI runner for a single agent run in the cursor-vs-RA pilot.

Spawn one serena MCP server, run one investigation, write trace artifacts to a per-run
directory under ``--output-dir`` (default: ``docs/cursor-vs-ra/runs/``).

Examples::

    python -m scripts.cursor_vs_ra.runner --arm cursor --run 1
    python -m scripts.cursor_vs_ra.runner --arm ra --run 2 --max-iters 60

The directory layout per run is::

    docs/cursor-vs-ra/runs/{timestamp}_{task}_{arm}_run{n}/
        trace.jsonl   # canonical event log
        trace.md      # human-readable render
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from scripts.cursor_vs_ra.agent import (
    AgentConfig,
    CursorVsRAAgent,
    extract_prompt_body,
)
from scripts.cursor_vs_ra.tool_surfaces import ARMS
from scripts.cursor_vs_ra.trace import TraceWriter

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROMPT_PATH = REPO_ROOT / "docs" / "cursor-vs-ra" / "templates" / "agent-prompt.md"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "docs" / "cursor-vs-ra" / "runs"
DEFAULT_PROJECT_PATH = Path("/Users/asher/Projects/iina")
DEFAULT_MODEL = "claude-opus-4-7"
DEFAULT_MAX_ITERS = 80
DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_TOKENS = 8192
DEFAULT_TASK = "iina-5909"
CONFIG_DIR = Path(__file__).resolve().parent / "configs"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--arm", required=True, choices=sorted(ARMS.keys()),
        help="Which paradigm arm to run.",
    )
    p.add_argument(
        "--run", type=int, required=True, dest="run_n",
        help="Run number within this arm/task (1, 2, 3, ...).",
    )
    p.add_argument(
        "--task", default=DEFAULT_TASK,
        help="Task identifier; appears in run dir name and trace header.",
    )
    p.add_argument(
        "--prompt-path", type=Path, default=DEFAULT_PROMPT_PATH,
        help="Path to agent prompt template (4-backtick fenced).",
    )
    p.add_argument(
        "--project-path", type=Path, default=DEFAULT_PROJECT_PATH,
        help="Path to the project (e.g. iina) the agent will navigate.",
    )
    p.add_argument(
        "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
        help="Directory under which run artifacts are written.",
    )
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--max-iters", type=int, default=DEFAULT_MAX_ITERS)
    p.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    arm = ARMS[args.arm]
    context_yaml = CONFIG_DIR / f"{arm.name}_arm.yml"
    if not context_yaml.exists():
        raise FileNotFoundError(f"missing arm context YAML: {context_yaml}")

    agent_prompt = extract_prompt_body(args.prompt_path)

    # one run dir per invocation; timestamp ensures monotonic naming even at repeated --run N
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = args.output_dir / f"{timestamp}_{args.task}_{arm.name}_run{args.run_n}"
    trace = TraceWriter(run_dir)

    config = AgentConfig(
        arm=arm,
        context_yaml_path=context_yaml,
        project_path=args.project_path,
        model=args.model,
        temperature=args.temperature,
        max_iters=args.max_iters,
        max_tokens=args.max_tokens,
        agent_prompt=agent_prompt,
        task=args.task,
        run_n=args.run_n,
    )

    agent = CursorVsRAAgent(config, trace)
    result = asyncio.run(agent.run())

    md_path = trace.render_markdown()

    print()
    print(f"Run complete: {result.outcome}")
    print(f"  Turns:    {result.total_turns}")
    print(f"  Wall:     {result.wall_time_s:.1f}s")
    u = result.total_usage
    print(
        f"  Tokens:   in={u.input_tokens}, out={u.output_tokens}, "
        f"cache_create={u.cache_creation_input_tokens}, cache_read={u.cache_read_input_tokens}"
    )
    print(f"  JSONL:    {trace.jsonl_path}")
    print(f"  Markdown: {md_path}")
    if result.error:
        print(f"  Error:    {result.error}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
