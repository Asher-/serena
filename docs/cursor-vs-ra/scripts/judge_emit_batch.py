#!/usr/bin/env python3
"""Generate an orchestrate batch yaml for the phase-(c) blind A/B judge run.

Renders the six pilot session JSONLs through render_trace.render() with
--redact, builds the nine cross-arm pairs (3 cursor x 3 ra), randomly
assigns A/B order per pair (seeded), and emits one orchestrate task per
pair. Each task carries a prompt = blind-judge-template + Trace A +
Trace B and a findings path = verdicts/<pair_id>.md. The orchestrator
preamble adds a "use the Write tool, file is the deliverable" rider, so
the judge writes its TRACE A NOTES / TRACE B NOTES / VERDICT / RATIONALE
straight to that file.

Per blind-judge-prompt.md operator notes:
- Same model + temperature for all pairs (judge identity fixed). We pin
  Opus 4.7 (claude-opus-4-7) so reproducibility is stable across runs.
- TIE counts as non-vote in headline rate (handled by the aggregator).
- Trace order randomized per pair. Aggregator joins manifest.json's
  cursor_was_a back to verdict A/B to produce cursor-vs-ra preference.
- Stateless per-pair API calls satisfy "judge does not see the same task
  twice" naturally — each task is a fresh CLI session.

Usage:
  python judge_emit_batch.py --out-dir docs/cursor-vs-ra/runs/judge/<ts>

Outputs (under --out-dir):
  batch_judge.yaml           # orchestrate input
  manifest.json              # pair metadata for the aggregator
  judge-input/<pair>.md      # exact prompt sent to each judge (audit)
  verdicts/                  # empty; the orchestrate run populates it
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import render_trace  # noqa: E402


PILOT_RUNS = [
    # cursor JSONLs are from the 2026-04-28 v3f sequential pilot. v3f re-ran
    # the cursor arm via claude-server orchestrate after the bridge.go HOME
    # override + paired --permission-mode=bypassPermissions landed (see
    # memory://serena/project/cursor-vs-ra-v3-result-and-9-0-blocker). All
    # three cursor sessions wrote findings.md cleanly with zero hook errors
    # and zero permission prompts. The same FALSIFICATION TEST gate from
    # v3e was kept in batch_cursor.yaml so the cursor traces still trace
    # both the seek-side enabler AND the post-seek cascade trigger and rule
    # out an alternative. RA JSONLs unchanged from the 9-0 baseline (RA arm
    # was not re-run).
    {"id": "cursor-0", "arm": "cursor",
     "jsonl": "/Users/asher/.claude/projects/-Users-asher-Projects-iina/e5ed03bb-1fcd-4c70-9ed5-4ae6ba188c17.jsonl"},
    {"id": "cursor-1", "arm": "cursor",
     "jsonl": "/Users/asher/.claude/projects/-Users-asher-Projects-iina/136cb40f-c286-4c88-a7ee-fcfe6edb78e6.jsonl"},
    {"id": "cursor-2", "arm": "cursor",
     "jsonl": "/Users/asher/.claude/projects/-Users-asher-Projects-iina/398a13ac-a87f-496b-8f14-add8833374c8.jsonl"},
    {"id": "ra-0", "arm": "ra",
     "jsonl": "/Users/asher/.claude/projects/-Users-asher-Projects-iina/777ca131-5f25-4f66-a6d8-721beb840fc8.jsonl"},
    {"id": "ra-1", "arm": "ra",
     "jsonl": "/Users/asher/.claude/projects/-Users-asher-Projects-iina/006fe917-8776-4ab9-bc00-d826f4721491.jsonl"},
    {"id": "ra-2", "arm": "ra",
     "jsonl": "/Users/asher/.claude/projects/-Users-asher-Projects-iina/15ab7fff-d78f-4627-baf5-e04ae5f7b98f.jsonl"},
]

DEFAULT_TEMPLATE = "/Users/asher/Dropbox/Projects/claude/serena/docs/cursor-vs-ra/templates/blind-judge-prompt.md"
DEFAULT_MODEL = "claude-opus-4-7"
DEFAULT_SEED = 42
DEFAULT_TOPIC_ROOT = "topic://project.serena.cursor-vs-ra"


def extract_template_body(template_path: Path) -> str:
    text = template_path.read_text()
    m = re.search(r"^````\s*\n(.+?)\n````\s*$", text, re.MULTILINE | re.DOTALL)
    if not m:
        raise ValueError("no ````-fenced prompt body in " + str(template_path))
    return m.group(1).rstrip() + "\n"


def render_pilot(jsonl_path: Path, max_result_chars: int) -> str:
    events = render_trace.load_jsonl(jsonl_path)
    return render_trace.render(events, redact=True, cap=max_result_chars)


def build_pairs(cursor_runs, ra_runs, seed):
    pairs = []
    rng = random.Random(seed)
    for c in cursor_runs:
        for r in ra_runs:
            cursor_was_a = rng.random() < 0.5
            pair_id = "pair-{:02d}-{}-vs-{}".format(len(pairs), c["id"], r["id"])
            pairs.append({
                "pair_id": pair_id,
                "cursor_id": c["id"],
                "ra_id": r["id"],
                "cursor_was_a": cursor_was_a,
                "trace_a_id": c["id"] if cursor_was_a else r["id"],
                "trace_b_id": r["id"] if cursor_was_a else c["id"],
            })
    return pairs


def build_prompt(template_body: str, trace_a_md: str, trace_b_md: str) -> str:
    return (
        template_body
        + "\n---\n\n# Trace A\n\n"
        + trace_a_md.rstrip()
        + "\n\n---\n\n# Trace B\n\n"
        + trace_b_md.rstrip()
        + "\n"
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out-dir", type=Path, required=True,
                    help="Run output directory (e.g. docs/cursor-vs-ra/runs/judge/20260427T200000Z)")
    ap.add_argument("--template", type=Path, default=Path(DEFAULT_TEMPLATE))
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help="Full model id (default %(default)s for stable reproducibility; use 'opus' for the alias)")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--max-result-chars", type=int, default=0,
                    help="Cap each tool-result render at N chars (0=unlimited)")
    ap.add_argument("--coordinator", default=None,
                    help="Coordinator id (default: derived from out-dir name)")
    ap.add_argument("--topic-root", default=DEFAULT_TOPIC_ROOT)
    ap.add_argument("--project", default="/tmp",
                    help="Project working directory for the spawned CLI sessions (default %(default)s; nothing in this dir is read)")
    args = ap.parse_args()

    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    inputs_dir = out_dir / "judge-input"
    verdicts_dir = out_dir / "verdicts"
    inputs_dir.mkdir(exist_ok=True)
    verdicts_dir.mkdir(exist_ok=True)

    coordinator = args.coordinator or ("cursor-vs-ra-judge-" + out_dir.name)

    template_body = extract_template_body(args.template)

    cursor_runs = [r for r in PILOT_RUNS if r["arm"] == "cursor"]
    ra_runs = [r for r in PILOT_RUNS if r["arm"] == "ra"]

    sys.stderr.write("Rendering 6 pilot traces...\n")
    rendered = {}
    for r in PILOT_RUNS:
        md = render_pilot(Path(r["jsonl"]), max_result_chars=args.max_result_chars)
        rendered[r["id"]] = md
        sys.stderr.write("  " + r["id"] + ": " + str(len(md)) + " chars\n")

    pairs = build_pairs(cursor_runs, ra_runs, args.seed)

    tasks = []
    for pair in pairs:
        prompt = build_prompt(
            template_body,
            rendered[pair["trace_a_id"]],
            rendered[pair["trace_b_id"]],
        )
        verdict_path = verdicts_dir / (pair["pair_id"] + ".md")
        # Persist the exact prompt sent (judge-input/) for audit; the judge
        # also produces verdicts/<pair>.md when it Writes the file.
        (inputs_dir / (pair["pair_id"] + ".md")).write_text(prompt)
        tasks.append({
            "id": pair["pair_id"],
            "roots": [args.topic_root],
            "findings": str(verdict_path),
            "skip_preamble": True,
            "options": {
                # Minimal blast radius. We deliberately OMIT --allowedTools.
                # On 2026-04-28 we discovered that passing `--allowedTools Write`
                # alongside `--settings <path>` causes claude CLI to ignore the
                # settings-file's hooks override (judges hit user-level
                # require-authority-read.sh despite an empty PreToolUse: []
                # in judge.json). With --allowedTools omitted the override
                # works: builtin_tools="Write" restricts the BUILTIN set to Write,
                # and no --mcp-config means no MCP tools are available, so no
                # whitelist is needed.
                "builtin_tools": "Write",
                "model": args.model,
                # Override user-level SessionStart and PreToolUse hooks so the
                # judge's verdict context isn't polluted with ~180KB of
                # brain-context and so the require-authority-read.sh /
                # block-source-reads.sh hooks don't fire. See
                # memory://serena/project/claude-cli-mcp-tools-need-project-settings-override.
                "settings": "/Users/asher/.claude/orchestrate-settings/judge.json",
            },            "prompt": prompt,
        })

    batch = {
        "project": args.project,
        "coordinator": coordinator,
        "topic_root": args.topic_root,
        "tasks": tasks,
    }

    batch_path = out_dir / "batch_judge.yaml"
    with batch_path.open("w") as f:
        yaml.safe_dump(batch, f, default_flow_style=False, allow_unicode=True, width=10**9)

    manifest = {
        "out_dir": str(out_dir),
        "coordinator": coordinator,
        "model": args.model,
        "seed": args.seed,
        "topic_root": args.topic_root,
        "template": str(args.template),
        "pairs": pairs,
        "trace_chars": {r["id"]: len(rendered[r["id"]]) for r in PILOT_RUNS},
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    sys.stderr.write("Wrote " + str(batch_path) + "\n")
    sys.stderr.write("Wrote " + str(out_dir / "manifest.json") + "\n")
    sys.stderr.write("Pair count: " + str(len(pairs)) + "\n")
    sys.stderr.write("Total prompt chars: " + str(sum(len(t["prompt"]) for t in tasks)) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
