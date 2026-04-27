#!/usr/bin/env python3
"""Aggregate phase-(c) blind A/B judge verdicts into preference statistics.

Reads manifest.json (per-pair metadata, including which of A/B is cursor)
and verdicts/<pair_id>.md (the file each judge wrote via the Write tool),
parses the four operator-prescribed fields (TRACE A NOTES, TRACE B NOTES,
VERDICT, RATIONALE), translates the judge's blind A/B verdict back into
cursor-vs-ra preference using cursor_was_a, and emits:

  - results.jsonl: one row per pair
  - summary.md:    headline cursor preference rate (excluding TIE),
                   separate TIE rate, per-pair table

Headline rate = cursor_wins / (cursor_wins + ra_wins). TIE counts as a
non-vote and is reported separately, per blind-judge-prompt operator notes.

Usage:
  python judge_aggregate.py --run-dir docs/cursor-vs-ra/runs/judge/<ts>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

VERDICT_PATTERN = re.compile(r"VERDICT\s*:\s*(A|B|TIE)\b", re.IGNORECASE)
NOTES_A_PATTERN = re.compile(
    r"TRACE\s+A\s+NOTES\s*:\s*(.*?)"
    r"(?=\n\s*[\-\*•\#]?\s*\*?\*?TRACE\s+B\s+NOTES|\n\s*[\-\*•\#]?\s*\*?\*?VERDICT|\Z)",
    re.IGNORECASE | re.DOTALL,
)
NOTES_B_PATTERN = re.compile(
    r"TRACE\s+B\s+NOTES\s*:\s*(.*?)"
    r"(?=\n\s*[\-\*•\#]?\s*\*?\*?VERDICT|\n\s*[\-\*•\#]?\s*\*?\*?RATIONALE|\Z)",
    re.IGNORECASE | re.DOTALL,
)
RATIONALE_PATTERN = re.compile(r"RATIONALE\s*:\s*(.*)\Z", re.IGNORECASE | re.DOTALL)


def parse_verdict_file(text: str):
    notes_a_m = NOTES_A_PATTERN.search(text)
    notes_b_m = NOTES_B_PATTERN.search(text)
    verdict_m = VERDICT_PATTERN.search(text)
    rationale_m = RATIONALE_PATTERN.search(text)
    return {
        "notes_a": notes_a_m.group(1).strip() if notes_a_m else None,
        "notes_b": notes_b_m.group(1).strip() if notes_b_m else None,
        "verdict": verdict_m.group(1).upper() if verdict_m else None,
        "rationale": rationale_m.group(1).strip() if rationale_m else None,
    }


def cursor_won(verdict, cursor_was_a):
    if verdict is None or verdict == "TIE":
        return None
    if verdict == "A":
        return cursor_was_a
    if verdict == "B":
        return not cursor_was_a
    return None


def aggregate(records):
    cursor_wins = sum(1 for r in records if r.get("cursor_won") is True)
    ra_wins = sum(1 for r in records if r.get("cursor_won") is False)
    ties = sum(1 for r in records if r.get("verdict") == "TIE")
    parse_failures = sum(1 for r in records if r.get("verdict") not in ("A", "B", "TIE"))
    non_tie = cursor_wins + ra_wins
    return {
        "n": len(records),
        "cursor_wins": cursor_wins,
        "ra_wins": ra_wins,
        "ties": ties,
        "parse_failures": parse_failures,
        "non_tie_n": non_tie,
        "cursor_preference_rate": (cursor_wins / non_tie) if non_tie else None,
        "tie_rate": (ties / len(records)) if records else 0.0,
    }


def write_summary_md(out_path: Path, records, agg, manifest):
    rate = agg["cursor_preference_rate"]
    rate_str = ("{:.3f}".format(rate)) if rate is not None else "n/a (no non-TIE outcomes)"
    lines = [
        "# Phase (c) blind A/B judge results",
        "",
        "- Coordinator: " + manifest.get("coordinator", "(unknown)"),
        "- Model: " + manifest.get("model", "(unknown)"),
        "- Seed: " + str(manifest.get("seed", "(unknown)")),
        "- Pairs: " + str(agg["n"]),
        "- Cursor wins: " + str(agg["cursor_wins"]),
        "- RA wins: " + str(agg["ra_wins"]),
        "- Ties: " + str(agg["ties"]),
        "- Parse failures: " + str(agg["parse_failures"]),
        "- Headline cursor preference rate (non-TIE): " + rate_str,
        "- Tie rate: " + "{:.3f}".format(agg["tie_rate"]),
        "",
        "## Per-pair",
        "",
        "| pair | cursor=A? | verdict | cursor_won |",
        "|------|-----------|---------|------------|",
    ]
    for r in records:
        lines.append(
            "| " + r["pair_id"]
            + " | " + ("yes" if r["cursor_was_a"] else "no")
            + " | " + str(r.get("verdict") or "?")
            + " | " + str(r.get("cursor_won"))
            + " |"
        )
    out_path.write_text("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run-dir", type=Path, required=True,
                    help="Run directory containing manifest.json and verdicts/")
    args = ap.parse_args()

    run_dir = args.run_dir.resolve()
    manifest_path = run_dir / "manifest.json"
    verdicts_dir = run_dir / "verdicts"
    if not manifest_path.exists():
        sys.stderr.write("error: " + str(manifest_path) + " missing\n")
        return 2
    if not verdicts_dir.exists():
        sys.stderr.write("error: " + str(verdicts_dir) + " missing\n")
        return 2

    manifest = json.loads(manifest_path.read_text())
    pairs = manifest["pairs"]

    records = []
    for pair in pairs:
        verdict_file = verdicts_dir / (pair["pair_id"] + ".md")
        if not verdict_file.exists():
            sys.stderr.write("warn: " + pair["pair_id"] + " verdict file missing\n")
            records.append({
                "pair_id": pair["pair_id"], "cursor_id": pair["cursor_id"], "ra_id": pair["ra_id"],
                "cursor_was_a": pair["cursor_was_a"],
                "trace_a_id": pair["trace_a_id"], "trace_b_id": pair["trace_b_id"],
                "verdict": None, "cursor_won": None,
                "error": "verdict file missing",
                "ts": datetime.now(timezone.utc).isoformat(),
            })
            continue
        text = verdict_file.read_text()
        parsed = parse_verdict_file(text)
        won = cursor_won(parsed["verdict"], pair["cursor_was_a"])
        records.append({
            "pair_id": pair["pair_id"], "cursor_id": pair["cursor_id"], "ra_id": pair["ra_id"],
            "cursor_was_a": pair["cursor_was_a"],
            "trace_a_id": pair["trace_a_id"], "trace_b_id": pair["trace_b_id"],
            "verdict": parsed["verdict"], "cursor_won": won,
            "notes_a": parsed["notes_a"], "notes_b": parsed["notes_b"],
            "rationale": parsed["rationale"],
            "verdict_file": str(verdict_file),
            "ts": datetime.now(timezone.utc).isoformat(),
        })

    agg = aggregate(records)

    results_path = run_dir / "results.jsonl"
    with results_path.open("w") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    summary_path = run_dir / "summary.md"
    write_summary_md(summary_path, records, agg, manifest)

    sys.stdout.write(json.dumps(agg, indent=2) + "\n")
    sys.stderr.write("Wrote " + str(results_path) + "\n")
    sys.stderr.write("Wrote " + str(summary_path) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
