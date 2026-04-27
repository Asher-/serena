# Issue #5909 — Oracle Pass

Phase (a) of the post-pilot 4-phase analysis. Scores all 6 pilot findings against the actual IINA source as of `develop` @ `eea7b809` (2026-04-27).

**No upstream fix exists for #5909.** The last commit referencing `5909` in IINA's git history is `f31cd1b0` (2020-01-10), unrelated. So "ground truth" here is the oracle author's analysis of the current code, not an authoritative patch. Where the analysis is ambiguous, the oracle says so explicitly rather than picking arbitrarily.

## Ground-truth analysis

The bug arises from the interaction of two distinct defects in the code as it stands today.

### Defect A — the EOF-percent clamp leaks past EOF

`iina/PlayerCore.swift:952-964`:

```swift
func seek(percent: Double, forceExact: Bool = false) {
  var percent = percent
  // mpv will play next file automatically when seek to EOF.
  // We clamp to a Range to ensure that we don't try to seek to 100%.
  // however, it still won't work for videos with large keyframe interval.
  if let duration = info.videoDuration?.second, duration > 0 {
    percent = percent.clamped(to: 0..<100)        // line 959
  }
  let useExact = forceExact ? true : Preference.bool(for: .useExactSeek)
  let seekMode = useExact ? "absolute-percent+exact" : "absolute-percent"
  mpv.command(.seek, args: ["\(percent)", seekMode], checkError: false, level: .verbose)
}
```

Via `FloatingPoint.clamped(to: Range)` at `iina/Extensions.swift:412-421` — which returns `range.upperBound.nextDown` for `self >= upperBound` — the clamp produces `99.99999999999999` for any input ≥ 100. The author already knows this won't work in all cases; the comment on line 956 says so. mpv treats the resulting seek target as EOF and auto-advances the playlist. This explains *why one playlist advance fires* per drag-tick that pegs the slider at maxValue.

### Defect B — `updatePlayTime` overwrites the slider mid-drag

`iina/PlayerWindowController.swift:606-633`:

```swift
func updatePlayTime(withDuration: Bool, andProgressBar: Bool) {
  guard loaded, player.info.state.loaded else { return }
  guard let duration  = player.info.videoDuration  else { ... return }
  guard let pos       = player.info.videoPosition  else { ... return }
  guard let remaining = player.info.videoRemaining else { ... return }
  [leftLabel, rightLabel].forEach { ... }
  player.touchBarSupport.touchBarPosLabels.forEach { ... }
  if andProgressBar {
    let percentage = (pos.second / duration.second) * 100
    playSlider.doubleValue = percentage                                          // line 630
    player.touchBarSupport.touchBarPlaySlider?.setDoubleValueSafely(percentage)  // line 631
  }
}
```

Line 630 writes `playSlider.doubleValue = percentage` unconditionally — including while `NSSliderCell` is mid-tracking. The touch-bar slider on line 631 routes through `TouchBarPlaySlider.setDoubleValueSafely`, which checks an `isTouching` flag (`iina/TouchBarSupport.swift:290-293`) and skips the write during active touch interaction. **The main slider has no analogous guard.** The project already has the concept of a per-drag guard against external writes — it just hasn't been applied on the AppKit-mouse path.

### How the cascade requires both defects

A single drag-tick that produces one near-EOF seek (Defect A) and one playlist advance is the user's *stated expectation*. The cascade — multiple advances per drag — forms because Defect B externally yanks `playSlider.doubleValue` back to ~0 every time `updatePlayTime` runs after a file change. The very next `mouseDragged` event re-snaps the value to maxValue from ~0, which is a real value change, which causes `NSSliderCell` to re-fire `playSliderChanges`, which produces a fresh near-EOF seek on the *new* file. The loop continues until mouse-up.

`NSSliderCell` in continuous mode (the default for `NSSlider`) fires the action **on value changes, not on every drag tick.** Without Defect B's external rewrite, the slider stays pinned at maxValue across the whole drag — no value change after the first action — and no further actions fire. With Defect B, every file change resets the slider to ~0 and the next tick re-snaps to maxValue, generating a fresh value-change action.

This is critical for adjudicating fix viability: **fixing only Defect A** prevents EOF events and therefore stops the cascade — but it also removes the one user-expected file advance at end-of-drag. **Fixing only Defect B** preserves the user-expected single advance but stops the cascade. The slider-rewrite gate is the expected-behavior-preserving fix.

The dependence on `NSSliderCell` firing on value-change-only is load-bearing for the Defect-B-only fix. If empirically the cell fires on every drag tick at maxValue regardless, Defect B fix alone would not break the cascade. The agents uniformly assume value-change-only, consistent with Apple's documented `isContinuous=true` semantics; the oracle assumes the same but flags this as the load-bearing assumption.

## Scoring rubric

Four dimensions, each ✓ / ◐ / ✗:

- **Citation accuracy**: cited line numbers and function signatures match the actual file
- **Control-flow accuracy**: step-by-step cascade matches what the code does
- **Causal completeness**: identifies both Defect A and Defect B and attributes the cascade correctly to B
- **Fix viability**: suggested fix breaks the cascade *and* preserves user-expected end-of-drag single advance

## Per-finding scores

### cursor-0 — Slider rewrite primary, clamp as enabler

| Dimension | Score | Notes |
|-----------|-------|-------|
| Citation | ◐ | Cites line 631 for the slider write; actual is line 630. All other citations (PlayerCore.swift:952-963 clamp, Extensions.swift:413-420, PlaySliderCell.swift:104, 175, etc.) are accurate. |
| Control flow | ✓ | 8-step trace matches code. |
| Causal completeness | ✓ | Best in the set. Identifies clamp as the per-tick EOF trigger, slider rewrite as the cascade engine. Distinguishes "single advance (expected)" from "cascade (bug)". |
| Fix viability | ✓ | `isDragging` flag in `PlaySliderCell` (set in `startTracking`, cleared in `stopTracking`) + gate the line-630 assignment on `!isDragging`. Preserves user-expected single advance. |

### cursor-1 — Clamp primary

| Dimension | Score | Notes |
|-----------|-------|-------|
| Citation | ✓ | All accurate. |
| Control flow | ✓ | 8-step trace matches code. |
| Causal completeness | ◐ | Clamp-focused. Step 6 mentions `syncUI(.time)` resetting the slider to ~0 but routes it as a downstream consequence, not a co-cause of the cascade. |
| Fix viability | ◐ | Replace percent-domain clamp with duration-domain (`min(percent / 100 * duration, duration - 1.0)`) routed through `seek(absoluteSecond:)`. Breaks the cascade. **Also removes the user-expected end-of-drag advance** — drag past right edge now just clamps to (duration-1) seconds with no auto-advance. |

### cursor-2 — Clamp primary

| Dimension | Score | Notes |
|-----------|-------|-------|
| Citation | ✓ | All accurate. |
| Control flow | ✓ | 8-step trace matches code. Correctly notes the `state != .loading` guard in `MainWindowController.playSliderChanges`. |
| Causal completeness | ◐ | Clamp-focused. Step 7 mentions `updatePlayTime` writing to ~0 but doesn't elevate it. **Secondary suggestion** is interesting: capture playlist position in `startTracking`, bail out of `playSliderChanges` on identity mismatch, clear in `stopTracking` — this is a structurally sound alternative path that targets the cascade directly. |
| Fix viability | ◐ | Primary fix: same duration-aware clamp as cursor-1; same UX caveat. Secondary fix (per-drag file binding) would break the cascade without changing user-expected behavior; this is independently viable. |

### ra-0 — Clamp primary

| Dimension | Score | Notes |
|-----------|-------|-------|
| Citation | ✓ | Accurate. Cites lines 957-960 — narrow window; the function spans 952-964 (the clamp expression itself is on line 959). Acceptable. |
| Control flow | ✓ | 8-step trace matches code. Goes deeper into mpv internals (`MPV_EVENT_END_FILE`, `MPVController.handleEvent` at 1186-1204). |
| Causal completeness | ◐ | Clamp-focused. Step 6 mentions slider write to ~0 in `updatePlayTime` but treats as downstream. |
| Fix viability | ◐ | Duration-aware clamp `[0...(safe / duration * 100)]` with `safe = max(0, duration - 1.0)`. Same UX caveat. Secondary IBAction short-circuit on `sender.doubleValue` unchanged is interesting but largely redundant with `NSSliderCell`'s own value-change detection. |

### ra-1 — Clamp primary, near-miss on the slider-rewrite insight

| Dimension | Score | Notes |
|-----------|-------|-------|
| Citation | ✓ | All accurate. |
| Control flow | ✓ | 9-step trace matches code. Most thorough mpv-side detail (`MPV_EVENT_END_FILE` → `fileEnded`; `keep-open=yes` semantics with `playlistAutoPlayNext`). |
| Causal completeness | ◐ | Clamp-focused. **But explicitly observes that `TouchBarPlaySlider.setDoubleValueSafely` guards on `isTouching` at TouchBarSupport.swift:290-293 to prevent "playback updates clobbering user-drag", and notes that no analogous gate exists on the seek side.** This is a near-miss of cursor-0 / ra-2's insight: ra-1 has the same observation but routes it as a secondary defense-in-depth rather than the root cause. |
| Fix viability | ◐ | Seconds-with-margin clamp issuing `absolute+exact` instead of `absolute-percent+exact`. Same UX caveat. Secondary `playSliderChanges` short-circuit (`guard sender.doubleValue < sender.maxValue else { return }`) is independently useful. |

### ra-2 — Slider rewrite primary, clamp as enabler

| Dimension | Score | Notes |
|-----------|-------|-------|
| Citation | ◐ | Cites line 629 for the slider write ("the unconditional `playSlider.doubleValue = percentage` write"); actual is line 630. All other citations accurate. |
| Control flow | ✓ | 9-step trace matches code. Identifies the full sync chain (`MPV_EVENT_PLAYBACK_RESTART` → `playbackRestarted` → `syncUI(.time)` → `updatePlayTime`). |
| Causal completeness | ✓ | Joint best with cursor-0. Identifies clamp as enabler, slider rewrite as cascade engine. Explicitly states *"A single near-EOF seek that triggers a playlist auto-advance is a known limitation … That alone would just match the reporter's stated expectation … one file advance, position resets to 0."* — the cleanest articulation of the expected-vs-bug distinction in the set. |
| Fix viability | ✓ | `isDraggingKnob` flag in `PlaySliderCell` (set in `startTracking`, cleared in `stopTracking`) + gate the line-630 assignment in `updatePlayTime`. Notes that AppKit's `cell.isHighlighted` is a viable alternative that requires no new state. Preserves user-expected single advance. |

## Summary table

| ID | Citation | Control flow | Causal completeness | Fix viability |
|----|----------|--------------|---------------------|---------------|
| cursor-0 | ◐ | ✓ | ✓ | ✓ |
| cursor-1 | ✓ | ✓ | ◐ | ◐ |
| cursor-2 | ✓ | ✓ | ◐ | ◐ |
| ra-0     | ✓ | ✓ | ◐ | ◐ |
| ra-1     | ✓ | ✓ | ◐ | ◐ |
| ra-2     | ◐ | ✓ | ✓ | ✓ |

Two findings (cursor-0, ra-2) reach the strongest causal model and propose the expected-behavior-preserving fix; both have a one-line off-by-one citation error on the line of the unconditional slider write. Four findings (cursor-1, cursor-2, ra-0, ra-1) identify a real defect (the percent clamp) and propose a viable but UX-changing fix.

## Convergence and divergence observations

### The 4-vs-2 split is uncorrelated with tool surface

| | Cursor arm | RA arm |
|---|---|---|
| Slider-rewrite primary | cursor-0 | ra-2 |
| Clamp primary | cursor-1, cursor-2 | ra-0, ra-1 |

Each arm produced one slider-rewrite finding (the better causal explanation) and two clamp findings, in the same 1:2 ratio. **Tool topology does not predict which root-cause hypothesis the agent lands on.** The divergence comes from agent reasoning patterns — whether the agent treats the cascade as a separable phenomenon worth labeling distinctly, or rolls it into the clamp story — not from which navigation tools were available.

### All 6 agents reach essentially the same control-flow model

The 8/9-step cascade is described in materially identical terms across all findings. The disagreement is purely about which step of the shared causal graph to label as "the root cause." Both arms identify the same code locations: PlayerCore.swift:952-964 (clamp), Extensions.swift:412-421 (the half-open-range clamped extension), PlayerWindowController.swift:606-632 (updatePlayTime, with playSliderChanges right below at 689-693), PlaySliderCell.swift:175-189 (start/stopTracking).

### Token cost (per pilot data)

- Cursor arm: 8.33M tokens across 3 sessions (mean ~2.78M / session)
- RA arm: 13.14M tokens across 3 sessions (mean ~4.38M / session)

RA used **1.58× more tokens** to reach the same control-flow model with the same 2:1 hypothesis split.

## What this oracle cannot adjudicate

- **`NSSliderCell.continueTracking` action-firing semantics.** The slider-rewrite-only fix's correctness depends on the cell firing the action on value change rather than on every drag tick. The agents uniformly assume value-change-only (consistent with Apple's documented `isContinuous=true` semantics). Empirical verification — instrumenting `playSliderChanges` to log mouseDragged-vs-action ratio at maxValue — would lift this from oracle assumption to ground truth.
- **Product judgment on user-expected behavior.** The bug report says drag-past-end should advance to next file. Whether this remains the desired UX after the cascade is fixed is a question for the IINA maintainers, not for code analysis.
- **Cross-callsite impact of the seconds-with-margin clamp fix.** Whether changing `seek(percent:)` to clamp in seconds breaks any other call site of `seek(percent:)` (chapter seek, keyboard shortcuts, etc.). A targeted xref on `seek(percent:` would settle it; this oracle does not run one.
