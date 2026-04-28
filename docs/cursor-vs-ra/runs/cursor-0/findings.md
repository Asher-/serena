# Bug Report: Scrub-by-Mouse-Drag Cascades Through Multiple Files (Issue #5909)

---

## ROOT CAUSE

**Dual-site bug spanning two functions:**

- **Seek-side enabler:** `PlayerCore.seek(percent:forceExact:)` — `iina/PlayerCore.swift:951–963`
- **Post-seek cascade trigger:** `PlayerWindowController.playSliderChanges(_:)` — `iina/PlayerWindowController.swift:689–693`  
  (also the weaker version of the same guard in `MainWindowController.playSliderChanges` — `iina/MainWindowController.swift:3248`)

**One-sentence root cause:** After a near-EOF drag-seek advances mpv to the next playlist file, the player's state transitions to `.starting` rather than a fully-stable state, but the IBAction guard in `playSliderChanges` permits seeks in `.starting`, so AppKit's continuous-action NSSlider re-fires the seek against the new file on every subsequent mouse-dragged event while the user is still holding the knob near the right end.

---

## CONTROL FLOW

Numbered steps tracing the cascade from drag event to bug:

1. **User mouseDown on slider knob near the right end.**  
   `PlaySliderCell.startTracking(at:in:)` (`iina/PlaySliderCell.swift:175–183`) fires.  
   It records `isPausedBeforeSeeking` and calls `playerCore.pause()`, putting the player into state `.paused`.

2. **User mouse-dragged; NSSlider (isContinuous = true by default) fires `playSliderChanges`.**  
   `MainWindowController.playSliderChanges(_:)` (`iina/MainWindowController.swift:3246–3262`) runs its guard:  
   `guard player.info.state.active, player.info.state != .loading else { return }`  
   `.paused` passes both predicates; `super.playSliderChanges(sender)` is called.  
   `PlayerWindowController.playSliderChanges(_:)` (`iina/PlayerWindowController.swift:689–693`) computes:  
   `let percentage = 100 * sender.doubleValue / sender.maxValue`  
   With slider at max (100.0/100.0), `percentage = 100.0`. Calls `player.seek(percent: 100.0, forceExact: ...)`.

3. **`PlayerCore.seek(percent:forceExact:)` (`iina/PlayerCore.swift:951–963`) applies the clamp:**  
   ```swift
   percent = percent.clamped(to: 0..<100)
   ```  
   `FloatingPoint.clamped(to: Range<Self>)` (`iina/Extensions.swift:413–421`) returns `100.0.nextDown ≈ 99.9999999999999`. This is passed to mpv as `absolute-percent` (keyframe-aligned) seek. For videos with a large keyframe interval, the nearest keyframe is at or past EOF; mpv advances to the next playlist entry. The code itself acknowledges this: *"it still won't work for videos with large keyframe interval."*

4. **mpv fires `MPV_EVENT_END_FILE` then `MPV_EVENT_START_FILE`.**  
   `MPVController.handleEvent(_:)` (`iina/MPVController.swift:1148`) dispatches to the main queue:  
   `player.info.state = .starting`  
   State is now `.starting` (raw value = 1; `active = rawValue < 5` = true; is not `.loading`).

5. **mpv fires `MPV_EVENT_SEEK`.**  
   `MPVController.handleEvent` (`iina/MPVController.swift:1162`) dispatches `player.syncUI(.time)` to main queue.  
   `PlayerCore.syncUI(.time)` (`iina/PlayerCore.swift:2663–2728`) calls `currentController.updatePlayTime(withDuration:andProgressBar:)`.  
   `PlayerWindowController.updatePlayTime` (`iina/PlayerWindowController.swift:606–633`) sets  
   `playSlider.doubleValue = percentage` — but this does NOT trigger the IBAction (see Falsification Test).

6. **mpv fires `MPV_EVENT_PLAYBACK_RESTART`.**  
   `MPVController.handleEvent` (`iina/MPVController.swift:1183`) dispatches `player.playbackRestarted()` then `player.syncUI(.time)`.  
   `PlayerCore.playbackRestarted()` (`iina/PlayerCore.swift:2302–2320`) calls `syncUI(.time)` again → another `updatePlayTime` → another `playSlider.doubleValue = percentage` write. Still does not trigger the IBAction.

7. **User is still dragging. AppKit fires the next `playSliderChanges` call.**  
   Mouse is still at or near the right end. AppKit's `NSSliderCell` tracking loop computes value from current mouse X-position (not from `doubleValue`), still yielding ~100.0.  
   `MainWindowController.playSliderChanges` guard: state is `.starting` → `state.active = true` AND `state != .loading = true` → guard PASSES.  
   Another seek to `100.0.nextDown` is sent to mpv.

8. **mpv advances to the next-next file. Steps 4–7 repeat for every file in the playlist.**

---

## WHY IT CASCADES

The cascade is driven by the interaction of two independent weaknesses. On the seek side, `seek(percent:)` clamps the input to `100.0.nextDown` (≈ 99.9999999999999%) and sends it as an `absolute-percent` (keyframe-aligned) command; for any video whose final keyframe is less than ~0.000001% before EOF — which covers all large-keyframe-interval videos and many ordinary ones — mpv treats this as EOF and auto-advances the playlist. That is the first advance. The cascade arises because nothing suppresses the IBAction after the advance: NSSlider is continuous (`isContinuous = true`), so it fires `playSliderChanges` on every `NSMouseDragged` event while the mouse button is held. After the advance the player state is `.starting`, but the guard `player.info.state.active, player.info.state != .loading` permits `.starting` (raw value 1 is active and is not `.loading`). Each iteration: drag event → seek → mpv EOF → next file starts → state `.starting` → drag event → seek → … This loop runs at the frequency of mouse-dragged events until the user releases the mouse or moves it away from the far-right position.

**FALSIFICATION TEST.** My hypothesis assigns the post-seek cascade trigger to AppKit's continuous-action firing loop (mouse events). The specific alternative I must rule out is: *`updatePlayTime`'s unconditional write `playSlider.doubleValue = percentage` (`iina/PlayerWindowController.swift:630`) during the drag triggers `playSliderChanges` via AppKit's value-change notification path, producing the re-fire.* If the proposed fix (tighter state guard) landed but `updatePlayTime` still wrote back to the slider during drag, would the cascade still occur?

**Answer: No.** `PlaySliderCell` (`iina/PlaySliderCell.swift:175–190`) overrides `startTracking` (line 175) and `stopTracking` (line 185) but does **not** override `continueTracking(last:current:in:)`. AppKit's default `NSSliderCell.continueTracking` implementation fires the action based on mouse-position deltas — it does not read `slider.doubleValue` and does not invoke the action when `doubleValue` is set programmatically. `NSControl.setDoubleValue(_:)` is a pure setter: it updates internal state and schedules a redraw, but does not post an action event. This is observable directly in the cell source: the only method that changes player state is `startTracking`'s `playerCore.pause()` call; `updatePlayTime`'s write-back at line 630 changes the knob's visual position but is invisible to the tracking loop. Therefore, if the seek-side guard were tightened so no seek reaches mpv at all, but `updatePlayTime` still wrote back, the cascade would not occur. Conversely, if the state guard were fixed but `updatePlayTime` still wrote back, the cascade would also not occur. The write-back is not the loop-closing mechanism; mouse events are.

---

## SUGGESTED FIX

**Fix the post-seek cascade trigger** by restricting `playSliderChanges` to fire seeks only in stable playback states. The minimal one-line change is in `PlayerWindowController.playSliderChanges` (`iina/PlayerWindowController.swift:690`):

```swift
// Before:
guard player.info.state.active else { return }

// After:
guard player.info.state.loaded else { return }
```

`PlayerState.loaded` (`iina/PlayerState.swift`) is defined as `active && rawValue >= PlayerState.loaded.rawValue`, which admits only `.loaded` (2), `.playing` (3), and `.paused` (4). The `.starting` state (raw value 1) does not satisfy this predicate, so seeks are blocked from the moment mpv fires `MPV_EVENT_START_FILE` (state → `.starting`) until `MPV_EVENT_FILE_LOADED` fires `fileLoaded()` which sets state to `.loaded`. This breaks the cascade loop: the first drag-to-EOF advance still occurs (from `.paused` state, which is `.loaded`-qualified), but every subsequent re-fire of `playSliderChanges` while the new file is loading finds `.starting` and returns immediately. Once the file loads and the player re-enters `.paused` (mpv preserves the pause flag across playlist transitions), normal slider seeking resumes. The same single-character change (`active` → `loaded`) also fixes the identical vulnerability in the mini player path, which also calls `PlayerWindowController.playSliderChanges` as its base-class IBAction. The `MainWindowController` guard at `iina/MainWindowController.swift:3248` should also be updated (`state != .loading` → `state.loaded`) for symmetry, but because `MainWindowController.playSliderChanges` calls `super`, fixing the base class guard is sufficient.
