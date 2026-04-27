# IINA issue #5909 — "Scrub by Mouse Drag Mishandles Next Video"

## ROOT CAUSE
`iina/PlayerWindowController.swift:625-630` (`updatePlayTime`) — the unconditional `playSlider.doubleValue = percentage` write runs even while the user is mid-drag, so each automatic mpv playlist advance rewrites the knob to the new file's near-zero position; on the next `mouseDragged` event NSSliderCell snaps the knob back to `maxValue`, AppKit fires `playSliderChanges` again, and the cycle repeats one file at a time.

The seek-side enabler is `iina/PlayerCore.swift:951-963` (`seek(percent:forceExact:)`), whose clamp is `percent.clamped(to: 0..<100)` and resolves to `100.nextDown` (≈ `99.99999999999999`) via the half-open-range overload at `iina/Extensions.swift:412-420`. As the source comment at `PlayerCore.swift:954-955` already admits, that value still "won't work for videos with large keyframe interval" — mpv treats it as EOF and advances. That single advance is *expected*; what the bug reports is the *cascade* of advances, and the cascade lives in the slider rewrite, not the clamp.

## CONTROL FLOW
1. User presses on the play slider knob and drags right. `PlaySliderCell.startTracking` (`PlaySliderCell.swift:174-182`) calls `playerCore.pause()` and returns `true`. AppKit puts the cell into tracking mode and sets `cell.isHighlighted = true`.
2. As the cursor moves past the right edge of the slider track, on every `mouseDragged` AppKit calls `NSSliderCell.continueTracking`, which clamps the computed value to `maxValue`. The slider's `doubleValue` jumps to `maxValue` and AppKit invokes the cell's action — `playSliderChanges`.
3. `PlayerWindowController.playSliderChanges` (`PlayerWindowController.swift:688-692`) computes `percentage = 100 * sender.doubleValue / sender.maxValue` (= 100) and calls `player.seek(percent: 100, forceExact: !followGlobalSeekTypeWhenAdjustSlider)`.
4. `PlayerCore.seek(percent:forceExact:)` (`PlayerCore.swift:951-963`) reduces the value to `100.nextDown` via `clamped(to: 0..<100)` and issues `mpv.command(.seek, args: ["99.999…", "absolute-percent+exact"])`.
5. mpv's exact seek lands at or after the file's last keyframe, the file ends, and (because `keep-open` is not active for playlist auto-advance) mpv loads the next playlist entry. Eventually `MPV_EVENT_PLAYBACK_RESTART` fires and is handled at `MPVController.swift:1169-1184`, which calls `player.playbackRestarted()` and `player.syncUI(.time)`. `playbackRestarted` itself (`PlayerCore.swift:2301-2319`) also calls `syncUI(.time)`.
6. `syncUI(.time)` (`PlayerCore.swift:2662-2693`) hops to the main thread and calls `currentController.updatePlayTime(withDuration: …, andProgressBar: true)`.
7. `updatePlayTime` (`PlayerWindowController.swift:606-632`) reads the *new* file's `pos` and `duration`, computes `percentage = (pos.second / duration.second) * 100` (≈ 0 because the new file just started), and unconditionally writes `playSlider.doubleValue = percentage` at line 629 — even though the user's drag is still in flight.
8. The user is still moving the mouse (it is held past the right edge of the track). On the next `mouseDragged`, `NSSliderCell.continueTracking` recomputes the value from the cursor position — `maxValue` again. Because the slider's value just got rewritten to ~0 in step 7, this is a *real* value change, so AppKit re-fires the action `playSliderChanges`.
9. Goto step 3, but `player` is now the next file in the playlist. Each iteration consumes one playlist entry, producing the observed "cascade through multiple files" while the user keeps dragging.

## WHY IT CASCADES
A single near-EOF seek that triggers a playlist auto-advance is a known limitation that the existing `clamped(to: 0..<100)` is *meant* to mitigate (and the source comment notes it does not fully succeed). That alone would just match the reporter's stated *expectation* — one file advance, position resets to 0. The reason the player keeps marching through subsequent files is that nothing tells `updatePlayTime` to leave the slider alone while the user is actively scrubbing. AppKit's `NSSliderCell` tracking model assumes the slider's value is a function of the mouse position alone for the duration of a drag; if external code reassigns `doubleValue` mid-tracking, the very next mouse-event tick re-snaps it to whatever the cursor now represents, and AppKit treats that as a fresh user-driven change and re-fires the action. So the playback restart of the new file repaints the knob to ~0, the user's still-clamped cursor coordinate maps to `maxValue` on the next tick, the action fires again, the next seek hits the new file's near-EOF, the playlist advances again — ad infinitum until the user releases the mouse. The bug is the absence of a "user is scrubbing, do not externally rewrite the slider" guard, not the seek clamp itself.

## SUGGESTED FIX
Suppress the slider rewrite in `updatePlayTime` while the play slider is being tracked. Concretely, give `PlaySliderCell` an `isDraggingKnob` flag that is set to `true` in `startTracking` (`PlaySliderCell.swift:174-182`) and cleared in `stopTracking` (`PlaySliderCell.swift:184-189`), then change `PlayerWindowController.updatePlayTime` line 629 from the unconditional `playSlider.doubleValue = percentage` to something like:

```swift
if !(playSlider.cell as? PlaySliderCell)?.isDraggingKnob ?? false { ... } 
// or, equivalently, leveraging AppKit's built-in tracking flag:
if !(playSlider.cell?.isHighlighted ?? false) { playSlider.doubleValue = percentage }
```

With that guard, after the first auto-advance the slider stays pinned at `maxValue` (because the user's cursor is still past the track), `NSSliderCell.continueTracking` computes the same value on every subsequent tick, AppKit detects no value change, and `playSliderChanges` does not re-fire — so only one file advance happens and the cascade is broken. The touch-bar `setDoubleValueSafely(percentage)` call at `PlayerWindowController.swift:631` is independent and can stay as-is.

DONE

