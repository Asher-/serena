# Blind-judge head-to-head prompt

The judge (LLM or human) is presented with two traces — A and B — from agents investigating the same bug in the same codebase. Tool names have been redacted. The judge picks which trace is the more useful artifact, or rules tie. Verdicts feed the falsifiability prediction "cursor traces preferred ≥75% of the time" (claim 1: trace as artifact).

## Prompt

````
# Task

You are presented with two investigation traces — Trace A and Trace B. Both are by an LLM agent investigating the same bug in the same codebase. The agents had different tool sets, but you do not know which trace used which, and the tool names have been redacted.

Your task: judge which trace is the more useful artifact for someone who needs to continue this investigation cold.

# What "useful artifact" means

Imagine you are an engineer who has been handed one of these traces and told: "Pick this up where it left off. Don't re-investigate from scratch — use what's there." Which trace makes that easier?

Useful artifacts tend to:

- Make it clear what has been established and what is still uncertain
- Make it easy to re-find specific facts (which file, which function, which line)
- Show the agent's reasoning, not just queries and results
- Acknowledge dead ends and pivots openly
- Leave breadcrumbs that orient a fresh reader

Useful is *not* just:

- The trace that reached the "right" answer (correctness is judged separately)
- The shorter trace (efficiency is judged separately)
- The longer trace (length alone is not utility)
- The one with prettier formatting

# How to judge

1. Read both traces in full.
2. For each trace, write 2-3 sentences on its strengths and weaknesses as an artifact.
3. Pick: A more useful, B more useful, or roughly equivalent (TIE).
4. Write 3-4 sentences explaining your pick. Cite specific moments in the traces if relevant.

# Output format

- TRACE A NOTES: <2-3 sentences>
- TRACE B NOTES: <2-3 sentences>
- VERDICT: A | B | TIE
- RATIONALE: <3-4 sentences>

# What to ignore

- Length of trace (efficiency is measured separately)
- Whether the agent reached a confident final hypothesis (output quality is measured separately)
- Tool names, syntax, or formatting of tool calls (redaction may have left artifacts)

You are judging the trace as an artifact — would you rather pick this up, or the other one?
````

## Operator notes (not shown to the judge)

- **Trace order randomized per pair.** Cursor is not always A; flip a coin for each pair before sending. Otherwise the judge's order priors contaminate the result.
- **Tool-name redaction:** replace `cursor_*` and the RA tool names (`find_symbol`, `read_file`, etc.) with neutral placeholders (`tool_a`, `tool_b`, …) consistently within each trace. The redactor does not need to be perfect; the judge is told to ignore residual artifacts.
- **Judge identity must be fixed across all pairs.** Same LLM model, same temperature (≤0.3), same system context. Or, for human judges, the same individual judge for all pairs in a study.
- **Judge does not see the same task twice across pairs** — i.e. once a judge has read a cursor and an RA trace for issue #5909, do not show them another #5909 pair. Familiarity with the codebase contaminates subsequent judgments.
- **TIE counts as a non-vote** in the headline preference rate (numerator: A-or-B picks favoring cursor; denominator: A-or-B picks total, excluding TIEs). Report TIE rate separately so saturation is visible.

## Why allow TIE

If the cursor and RA arms are producing artifacts of equivalent utility, that is the finding — the strong form of claim 1 fails. Forcing a binary verdict would manufacture a winner that doesn't exist. The TIE rate itself is a useful signal: high TIE rate suggests the paradigms are converging on similar artifact quality and the trace-as-artifact claim is weak in this regime.
