# Navigation question bank

Five paradigm-agnostic questions asked of a reviewer (LLM or human) given a complete trace. Time-to-find is the per-question metric. Lower is better — high time-to-find indicates the trace is hard to navigate.

These questions are **pre-registered**: written before any pilot runs and not modified afterward. If a question turns out to be unanswerable from any well-formed trace, that is a finding about the question, not a license to swap it out.

## Questions

1. **What is the agent's final hypothesis about the root cause?**
   What file, what function, what line range, and what specifically goes wrong?

2. **Which functions or code locations did the agent investigate?**
   List them in the order they were investigated — one bullet per location.

3. **Did the agent ever change direction (abandon a hypothesis, pivot to a new line of inquiry)? If so, where in the trace, and what triggered the change?**

4. **What evidence does the agent cite for the final hypothesis?**
   Specific file:line references, control-flow observations, etc.

5. **At what point in the trace did the agent first identify the file containing the actual bug?**
   Cite the step number, tool call number, or message position.

## How to administer

For each question:

- Reviewer is given the trace and the question, with no prior knowledge of the trace's contents.
- Start a timer.
- Reviewer reads as much of the trace as they need to answer.
- Stop the timer when the reviewer states their answer.
- Record: trace ID, question number, time-to-find (seconds), answer correctness on a 3-point scale: correct / partial / incorrect-or-unfindable.

The same reviewer should answer all five questions on a given trace before moving to the next trace, to avoid familiarity effects within a single paradigm. The trace order should be randomized so the reviewer doesn't see all cursor traces first or all RA traces first.

## Answer key generation

For each pilot trace, an oracle pass (separate from the navigation test) records the ground-truth answer to each of the five questions, using whatever access the answer-key generator wants (read the trace fully, ask the original agent, etc.). Correctness scoring during the navigation test compares reviewer answers to the answer key.

## What this measures

This is the **navigation legibility** test. Two traces that contain the same information are not the same artifact if one of them takes 3× longer to find that information in. The trace-as-artifact claim predicts that cursor traces, by virtue of locality preservation and explicit position state, are easier to navigate.

If a cursor trace and an RA trace have the same time-to-find on every question, the trace-as-artifact claim is weakened — they are equally legible artifacts despite the topology difference. That would itself be a finding, and the presentation should report it.
