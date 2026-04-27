# Replay-from-halfway prompt

Fed to a fresh agent that has been given the first 50% of a completed trace and asked to continue. The replay agent gets the **same tool surface as the originating arm** — cursor-trace replays continue with cursor tools; RA-trace replays continue with RA tools. This is what makes the test fair: we are asking, given paradigm X's trace, how well does paradigm X bootstrap from it.

The harness slices the trace at 50% (by tokens — token-balanced cut keeps the replay test comparable across arms with different verbosity profiles) and substitutes the slice into the `<<< TRACE FIRST 50% >>>` placeholder. The original agent prompt is also embedded for reference.

## Prompt

````
You are continuing an investigation that another agent began. Below is the agent's trace so far — every tool call they made, every result they got, plus their reasoning. The investigation is into a bug in the IINA macOS video player. The original task description is appended at the bottom.

# Your task

Continue the investigation from where it stopped. You have the same tools the original agent used.

Goals (in priority order):

1. **Build on what is already there.** Do not re-investigate from scratch. The trace is real work; treat it as work product, not just history.
2. Reach a final hypothesis (using the same output format the original agent was asked for) — *or*, if the trace's path is clearly mistaken, redirect, but say so explicitly and explain why.
3. End your final message with the token DONE.

# What to do first

Read the trace. Identify:

- What has already been confirmed
- What was being investigated when the trace cut off
- What is the most useful next step given what is already known

Then take that next step.

# Trace

<<< TRACE FIRST 50% >>>

# Original task

<<< ORIGINAL AGENT PROMPT >>>
````

## Grading the replay

For each replay run, score on this rubric (operator-facing — not shown to the replay agent):

| Dimension | Scale | Definition |
|---|---|---|
| Built on prior work | 3-point: yes / partially / no | "yes" = first ≥3 actions extend the trace; "no" = first ≥3 actions repeat work the original agent already did |
| Reached final hypothesis | binary | Produced a `ROOT CAUSE:` block followed by `DONE` |
| Hypothesis matches original | 3-point: matches / compatible / contradicts | Only scored if both original and replay reached a hypothesis |
| Redirect was justified | N/A / justified / unjustified | Only scored if the replay redirected from the trace's direction |

**Productive continuation** = (built-on-prior is "yes") AND (reached-hypothesis is true). This is the binary used for the falsifiability prediction "cursor traces yield productive continuation ≥1.5× as often."

The replay grading is done by a fixed grader (LLM at low temperature, or human reviewer) blinded to which paradigm the trace came from. Trace IDs only — no paradigm labels.
