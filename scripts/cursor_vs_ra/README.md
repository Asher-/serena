# cursor-vs-RA pilot harness

Driver for the cursor-vs-random-access paradigm pilot. See
[`docs/cursor-vs-random-access.md`](../../docs/cursor-vs-random-access.md) for the
experimental design (thesis, sub-claims, falsifiability prereg).

## What it does

Spawns one serena MCP server (stdio) per run, configured via a per-arm `fixed_tools`
mode YAML so the published tool surface is exactly the arm's surface — and serena's
auto-generated system prompt naturally describes only those tools.

Drives an anthropic agent loop (Opus 4.7) against that server, with prompt caching on
the system prompt and the tools array. Streams every agent turn and every tool call
to a JSONL log; renders the same data to Markdown at the end.

## Tool surfaces

| Arm | Tools | Mode YAML |
| --- | --- | --- |
| `cursor` | `cursor_start`, `cursor_find`, `cursor_move`, `cursor_look`, `cursor_configure`, `cursor_overview`, `cursor_history`, `cursor_close` | [`configs/cursor_arm.yml`](configs/cursor_arm.yml) |
| `ra` | `find_symbol`, `find_referencing_symbols`, `get_symbols_overview`, `search_for_pattern` | [`configs/ra_arm.yml`](configs/ra_arm.yml) |

Both arms are read-only (no edit/insert/rename tools). The RA arm is "symbolic-RA"
— LSP-rooted, no raw byte reads — because no `ReadFileTool` exists in serena.

## Run a single agent

Set `ANTHROPIC_API_KEY`, then:

```bash
uv run python -m scripts.cursor_vs_ra.runner --arm cursor --run 1
uv run python -m scripts.cursor_vs_ra.runner --arm ra --run 1
```

Defaults: `--task iina-5909`, `--project-path /Users/asher/Projects/iina`,
`--model claude-opus-4-7`, `--max-iters 80`, `--temperature 0.7`,
`--max-tokens 8192`. Artifacts land at
`docs/cursor-vs-ra/runs/{timestamp}_{task}_{arm}_run{n}/{trace.jsonl,trace.md}`.

## Termination

- Agent emits literal `DONE` token in `end_turn` reply → `completed-with-done`
- Agent stops without `DONE` → `ended-without-done`
- `max_iters` reached without stop → `hit-cap`
- Harness/server/API exception → `error` (with details in `run_end.error`)

## Trace schema

JSONL events, one per line:

| `type` | When emitted | Key fields |
| --- | --- | --- |
| `run_start` | Once at startup | `arm`, `task`, `run_n`, `model`, `temperature`, `tool_names`, `agent_prompt_chars` |
| `agent_turn` | Per anthropic API response | `turn`, `stop_reason`, `usage`, `latency_ms`, `content` (assistant blocks) |
| `tool_call` | Per `tool_use` block dispatched | `turn`, `tool_use_id`, `tool_name`, `tool_input`, `result_text`, `is_error`, `latency_ms` |
| `run_end` | Once at shutdown | `outcome`, `total_turns`, `total_usage`, `wall_time_s`, `error` |

`usage` fields: `input_tokens`, `output_tokens`, `cache_creation_input_tokens`,
`cache_read_input_tokens` (taken verbatim from the anthropic API `response.usage`).

## Pilot batch

For the prereg pilot run ≥3 of each arm against the same task:

```bash
for n in 1 2 3; do
  uv run python -m scripts.cursor_vs_ra.runner --arm cursor --run "$n"
  uv run python -m scripts.cursor_vs_ra.runner --arm ra --run "$n"
done
```

Templates under [`docs/cursor-vs-ra/templates/`](../../docs/cursor-vs-ra/templates/)
are pre-registered and must not be edited after pilot runs begin.
