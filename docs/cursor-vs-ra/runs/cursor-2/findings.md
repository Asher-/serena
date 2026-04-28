# Bug Report: Scrub-by-Mouse-Drag Cascades Through Multiple Playlist Files (Issue #5909)

---

## ROOT CAUSE

Two cooperating defects form a closed loop:

- **Seek-side enabler** — `PlayerCore/seek(percent:forceExact:)` at `iina/PlayerCore.swift:951–963` clamps the seek percentage to `0..<100`, which via `FloatingPoint.clamped(to: Range<Self>)` at `iina/Extensions.swift:413–421` (line 417: `return range.upperBound.nextDown`) yields `Double(100).nextDown ≈ 99.9999999999999858%`. For videos with large keyframe intervals the code itself acknowledges (line 955 comment) that this is insufficient: an exact-mode seek to that value can overshoot the last decodable frame and cause mpv to treat the seek as reaching EOF, automatically advancing the playlist.

- **Post-seek cascade trigger** — `PlayerWindowController/updatePlayTime` at `iina/PlayerWindowController.swift:606–633` (line 630: `playSlider.doubleValue = percentage`) writes the play slider's stored value unconditionally — no guard against an active drag. When the new file loads at position 0, this write resets the slider to 0% while the user's mouse is still physically at the right edge of the slider track. `NSSliderCell.continueTracking` compares the mouse-derived value (~100%) against the slider's stored `doubleValue` (now 0%), detects a change, and re-fires the IBAction — triggering another seek on the newly loaded file.

---

## CONTROL FLOW (cascade trace)

1. **User drags play slider to the right edge.**  
   `NSSliderCell` tracking loop fires IBAction → `MainWindowController/playSliderChanges` (`MainWindowController.swift:3246`) → `super.playSliderChanges(sender)` → `PlayerWindowController/playSliderChanges` (`PlayerWindowController.swift:689`):
   ```swift
   let percentage = 100 * sender.doubleValue / sender.maxValue   // = 100.0
   player.seek(percent: percentage, forceExact: ...)
   ```

2. **Seek is clamped to `nextDown(100)`.**  
   `PlayerCore/seek(percent:forceExact:)` (`PlayerCore.swift:957`):
   ```swift
   percent = percent.clamped(to: 0..<100)   // → 99.99999999999998...
   ```
   `FloatingPoint.clamped(to: Range<Self>)` (`Extensions.swift:417`) returns `range.upperBound.nextDown`. The resulting mpv command is `seek "99.99999999999999" "absolute-percent+exact"`.

3. **mpv crosses EOF.**  
   For a file whose last decodable frame is at a position < `nextDown(100)% × duration`, an exact seek overshoots and mpv's EOF handling fires. mpv auto-advances the playlist.

4. **mpv fires `MPV_EVENT_PLAYBACK_RESTART` for the new file.**  
   `MPVController/handleEvent` (`MPVController.swift:1170–1183`):
   ```swift
   case MPV_EVENT_PLAYBACK_RESTART:
       DispatchQueue.main.async { [self] in
           ...
           player.playbackRestarted()   // internally calls syncUI(.time)
           player.syncUI(.time)         // explicit second call
       }
   ```
   Both calls reach `updatePlayTime(withDuration:andProgressBar: true)`.

5. **`updatePlayTime` fires *during* drag tracking.**  
   The `DispatchQueue.main.async` block executes on the main thread inside the NSSlider tracking loop because GCD's main queue source is registered with `kCFRunLoopCommonModes`, which includes `.eventTracking` — the same mode NSSlider uses for drag tracking. This is confirmed by the project's own `Timer.scheduledTimerInCommonMode` helper (`Extensions.swift:992–995`, line 993: `RunLoop.main.add(timer, forMode: .common)`), which uses the identical mechanism.

6. **Slider is written to 0% — the cascade trigger.**  
   `PlayerWindowController/updatePlayTime` (`PlayerWindowController.swift:628–631`):
   ```swift
   if andProgressBar {
       let percentage = (pos.second / duration.second) * 100  // = 0% (new file at start)
       playSlider.doubleValue = percentage                      // ← resets to 0%
       ...
   }
   ```
   The user's mouse is still physically at the rightmost slider position (100%). The slider's stored `doubleValue` is now 0%.

7. **Tracking loop re-fires the IBAction without mouse movement.**  
   `NSSliderCell.continueTracking` (standard AppKit, not overridden in `PlaySliderCell` — confirmed by reading `PlaySliderCell.swift:11–191` in full) compares the mouse-position-derived value (100%) against the current `doubleValue` (0%). Finding a mismatch, it fires the action → step 1 repeats on the newly loaded file.

8. **Cascade continues** until the playlist is exhausted or the user releases the mouse.

---

## WHY IT CASCADES

When mpv rolls to the next file after an EOF-crossing seek, `MPV_EVENT_PLAYBACK_RESTART` dispatches a main-thread block (via `DispatchQueue.main.async`) that calls `updatePlayTime`. Because GCD's main queue fires in `.commonModes` (which includes `.eventTracking`), this block executes *inside* the NSSlider drag-tracking loop without waiting for the drag to finish. `updatePlayTime` unconditionally writes `playSlider.doubleValue = 0` (the new file's initial position). The NSSlider tracking loop then detects the stored-value change (0% ≠ the mouse-position value of ~100%) and immediately re-fires `playSliderChanges`. This re-fires another seek to `nextDown(100)%` on the new file, which in turn crosses EOF, fires another `PLAYBACK_RESTART`, resets the slider again, and so on — one full-loop iteration per playlist file.

**FALSIFICATION TEST.** My hypothesis is that the post-seek cascade is driven by the `updatePlayTime` write to `playSlider.doubleValue`. The specific alternative I must rule out is: *the cascade is driven purely by the user's active mouse movement* — i.e., each tiny drag event independently re-fires the action with 100%, without any role for the `doubleValue` reset.

To rule this out I examined `PlaySliderCell.swift:11–191` in full. `PlaySliderCell` does not override `continueTracking(last:current:in:)`. The inherited `NSSliderCell` implementation compares the newly-computed mouse-derived value against the cell's current `doubleValue`. Since 100% (from the slider's maxValue clamp) equals the `doubleValue` stored before any write (also 100% from the prior drag), a stationary mouse generates *no mismatch* and the action does *not* re-fire on its own. Only a leftward mouse movement would produce a value < 100%, which is below the EOF-crossing threshold and would not cascade. Therefore, *without the `doubleValue` reset the cascade would produce at most one playlist advance per drag gesture*. The reset is load-bearing.

**Ruling out the alternative:** If `updatePlayTime` was guarded (`!playSlider.isHighlighted`), could any other code write `playSlider.doubleValue = 0` during tracking and close the loop? The `syncUITimer` is the only other path; it calls `syncUITime → syncUI(.time) → updatePlayTime`. But the timer is stopped whenever the player is paused (`pauseChanged → refreshSyncUITimer` sets `useTimer = false` because `info.state == .paused`). The one exception is the immediate `syncUITime()` call inside `refreshSyncUITimer` when `!wasTimerRunning` (`PlayerCore.swift:2572`), which fires from `fileLoaded` and `playbackRestarted`. This is the exact same `updatePlayTime` codepath, controlled by the same guard. Adding `!playSlider.isHighlighted` to `updatePlayTime` at line 630 therefore suppresses all slider writes during tracking through every path, not just the `PLAYBACK_RESTART` path.

---

## SUGGESTED FIX

**Fix the cascade trigger in `PlayerWindowController/updatePlayTime` (`PlayerWindowController.swift:628–631`):**

Change:
```swift
if andProgressBar {
    let percentage = (pos.second / duration.second) * 100
    playSlider.doubleValue = percentage
```

To:
```swift
if andProgressBar {
    let percentage = (pos.second / duration.second) * 100
    if !playSlider.isHighlighted { playSlider.doubleValue = percentage }
```

`NSSliderCell.isHighlighted` (already used in `PlaySliderCell.drawKnobOnly` at `PlaySliderCell.swift:65`) is `true` whenever the cell is being tracked — exactly while the user is dragging. This one-line guard prevents the stored-value reset during drag, so `NSSliderCell.continueTracking` sees no mismatch and the IBAction does not re-fire after a playlist advance. The cascade loop is broken.

The fix belongs on the **cascade-trigger side** (`updatePlayTime`) rather than the seek side, because (a) it closes the loop that produces *repeated* advances, (b) the seek-side clamping is acknowledged to be inherently imprecise for large-keyframe-interval videos, and (c) suppressing the slider write during tracking is the correct general policy — the slider should reflect the user's drag position, not be overwritten by playback state callbacks while the user holds the knob.

The underlying seek-side issue (that `nextDown(100)%` can still overshoot EOF for large-keyframe-interval or duration-imprecise files) is a separate, pre-existing defect that this fix intentionally does not attempt to address in a single line.
