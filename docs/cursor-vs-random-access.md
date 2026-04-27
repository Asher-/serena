# Cursor vs Random-Access: Comparison Presentation

Working document for [PR #1331](https://github.com/oraios/serena/pull/1331). Empirical sections below are placeholders, to be filled by pilot runs.

## Thesis

The cursor and random-access (RA) paradigms differ in *who integrates*, and consequently in *what each paradigm leaves behind as an artifact*. RA puts Claude at the center of a star — every tool call returns through context, and Claude assembles the picture entirely in-flight. Cursor puts Claude on a walk through a graph — position narrows reports, and **the trace itself is the artifact**.

This reframing is load-bearing. The cursor's claim is not "I find answers faster" but "**the investigation I produce is itself a usable thing.**" A blind judge given two traces from the same task — one cursor, one RA — should be able to tell at a glance which is the more re-investigable, more legible, more navigable artifact.

The right comparison is therefore **process-vs-process, not output-vs-output**.

Four falsifiable sub-claims:

| # | Claim | Operational form |
|---|---|---|
| 1 | **Trace is artifact** (the headline) | Cursor traces are re-investigable; RA traces aren't |
| 2 | Efficiency | Cumulative tokens per trace-of-equal-utility is lower under cursor |
| 3 | Capability | Cursor sustains coherent multi-hop investigations longer |
| 4 | Output quality | Final answers are at least as grounded under cursor (sanity check, not headline) |

If any claim fails on the benchmark, the presentation reports that. Credibility comes from honest reporting.

## Metrics

Process-comparison first. Output-comparison demoted to sanity check.

### Direct head-to-head (the heart of the comparison)

| Test | What it measures |
|---|---|
| Blind judge picks more useful trace | Trace as artifact |
| Replay-from-halfway: feed first 50% to fresh agent, ask it to continue | How well the trace bootstraps a new context |
| Navigation test: ask reviewer to find specific facts in the trace, time-to-find | Trace legibility and structure |

### Computed structural metrics

| Metric | Captures |
|---|---|
| Locality between consecutive steps (graph distance) | Cursor preserves locality; RA doesn't |
| Re-fetch rate (% of symbols touched ≥2×) | RA forgets; cursor's history doesn't |
| Backtrack visibility (explicit `cursor_move` vs implicit silence in RA) | Process recoverability |
| Coverage map (which subsystems touched, in what order) | Investigation shape |

### Token economics

| Metric | Why |
|---|---|
| Cumulative tokens (input + output + results) | Efficiency claim |
| Tokens-to-first-useful-finding | Convergence speed |
| Trace-tokens / trace-utility-score | Density of artifact |

### Sanity check (output, not headline)

| Metric | Why |
|---|---|
| Hypothesis specificity (file/function/line cited) | Did the run produce something coherent? |
| Hypothesis plausibility (rough) | Did the run go off the rails? |

**Headline visual**: cumulative tokens (x) vs trace utility (y). One curve per paradigm. Trace utility is a normalized blend of head-to-head preference, replayability, and navigation score.

## Presentation outline

### 1 — Reframe the question
Maintainers asked "many ways to map a repo." Reframe: it's not about mapping; it's about *who integrates* — and consequently, *what artifact each paradigm leaves behind*.
- RA: star topology (Claude at center, every call returns through context)
- Cursor: walk topology (path through graph, locality preserved between calls)

### 2 — The two paradigms, diagrammed
Single slide. Star vs walk. Annotation: "Each RA query discharges into Claude's context. Each cursor move preserves locality and shrinks the report. The RA trace is a query log; the cursor trace is a navigation."

### 3 — Per-claim evidence (one slide each)
- **3a Trace as artifact** (headline): blind head-to-head. Side-by-side screenshots with paradigm labels redacted.
- **3b Efficiency**: Pareto chart per task — cumulative tokens vs trace utility.
- **3c Capability**: depth-vs-coherence curve, replay-from-halfway demo, multi-cursor showcase.
- **3d Output sanity**: hypothesis-quality bar chart. Both paradigms produce coherent outputs (or honest failure). Output is *not* the headline.

Each slide shows where cursor *loses* (e.g., trivial 1-hop lookups). Credibility comes from honest reporting.

### 4 — Headline visual
Pareto chart: cumulative tokens (x) vs trace utility (y), one curve per paradigm, averaged across tasks.
- Cursor Pareto-dominates → claim proven.
- Curves cross → the crossover *is* the finding (defines the regime where each paradigm wins).

### 5 — Falsifiability
- What would have disproved each claim (preregistered below)
- What was observed
- Where the cursor model is weak / future work

## Harness design

### Components

**Driver**: Python script. Loads task spec, runs LLM agent with restricted tool surface, logs every call, serializes trace.

**Two surfaces** (the only thing that differs across runs):
- RA: `find_symbol`, `find_referencing_symbols`, `get_symbols_overview`, `search_for_pattern`, `read_file`
- Cursor: `cursor_start`, `cursor_move`, `cursor_look`, `cursor_configure`, `cursor_find`, `cursor_overview`, `cursor_history`, `cursor_close`, plus edit/insert/rename family

**Per-call log**: `{tool, args_redacted, input_tokens, output_tokens, result_tokens, latency_ms, ts}`

**Per-run trace artifact**: structured JSONL + rendered "trace document" (human-readable) + computed structural metrics.

### Process-comparison runners

These run *after* both arms produce traces.

- **Replay-from-halfway**: snip trace at 50%, feed only the first half to a fresh agent, ask it to continue. Does the new agent productively continue, or get lost?
- **Navigation test**: present the trace to a fresh agent (or human reviewer), ask N specific questions ("which symbols did the original agent investigate?", "what was concluded about X?", "what was the last hypothesis?"). Time-to-find.
- **Blind head-to-head**: present both traces with paradigm labels redacted. Judge picks: "which is the more useful artifact for someone trying to continue this investigation?"
- **Structural metric computers**: scripts that read each trace and emit locality coefficient, re-fetch rate, backtrack-visibility count, coverage map.

### Variance control
- ≥3 runs per surface per task (different seeds)
- Same model, same context window, identical preamble — only the tool list differs
- Same blind judge across runs

### Templates

Pre-registered prompt templates live under [docs/cursor-vs-ra/templates/](cursor-vs-ra/templates/). Drafted before any pilot runs to lock criteria in advance.

| File | Purpose |
|---|---|
| [`agent-prompt.md`](cursor-vs-ra/templates/agent-prompt.md) | Task prompt fed to the agent at run start. Identical across both arms; only the tool surface differs. |
| [`replay-halfway-prompt.md`](cursor-vs-ra/templates/replay-halfway-prompt.md) | Continuation prompt for the replay-from-halfway test. Includes the operator-facing grading rubric for "productive continuation." |
| [`navigation-questions.md`](cursor-vs-ra/templates/navigation-questions.md) | Five paradigm-agnostic questions for the navigation legibility test. Time-to-find per question is the metric. |
| [`blind-judge-prompt.md`](cursor-vs-ra/templates/blind-judge-prompt.md) | Blind judge instructions for the head-to-head. TIE permitted; trace order randomized; tool names redacted; same judge across all pairs. |

Read-only surfaces are recommended for both arms during the pilot — the task is investigative and edit/insert/rename tools are a noise source. Fix-PR (optional kicker, step 8) is its own pass.

## Pilot — single task

**Task**: investigate [iina/iina#5909](https://github.com/iina/iina/issues/5909) — "Scrub by Mouse Drag Mishandles Next Video." Cross-file chain: drag event → scrub handler → playback control → playlist advance → end-of-file logic.

**Why this task**:
- Genuinely cross-file, multi-subsystem
- Third-party codebase (no self-reference; defuses cherry-picking objection)
- Open bug, real stakes — agents are doing genuine work
- Reporter has not pre-investigated; both arms get fresh codebase
- Symptom is reproducible; expected vs actual is unambiguous

**Codebase**: iina/iina (Swift, SourceKit-LSP — confirmed working in serena's cursor tools)

**Per arm**: 3 runs, identical prompt, only tool surface differs.

**Comparison runs**:
1. Blind head-to-head on each cursor/RA pair
2. Replay-from-halfway for one trace per arm
3. Navigation test (3 questions per trace)
4. Structural metrics on all 6 traces

**Optional kicker**: if either arm produces a credible root-cause chain, file a fix PR. The demo then ends with "and this PR fixes the bug."

## Falsifiability (preregister)

Pilot predictions:
- Blind head-to-head: cursor traces preferred ≥75% of the time
- Replay-from-halfway: cursor traces yield productive continuation ≥1.5× as often
- Navigation test time-to-find: cursor traces ≤0.5× RA time
- Re-fetch rate: cursor < 10%, RA > 30%
- Cumulative tokens at equivalent trace utility: cursor ≤ 0.7 × RA
- Output quality: cursor ≥ RA (sanity check; equality is acceptable)

If any prediction fails by ≥20%, treat as falsification of the strong form of that claim. Revisit design before scaling to the full suite.

## TODO
- [x] Draft prompt templates (agent / replay / navigation / blind-judge) — under docs/cursor-vs-ra/templates/
- [ ] Build harness driver
- [ ] Wire trace serializer (JSONL + rendered document)
- [ ] Build process-comparison runners (replay, nav-test, head-to-head, structural metrics)
- [ ] Run pilot on iina/iina#5909 (≥3 cursor, ≥3 RA)
- [ ] Run process-comparison runners across all pairs
- [ ] Fill in section 3a–3d with measured numbers
- [ ] Build slide visuals
- [ ] Pre-mortem with skeptical reader before publishing
