# Bug #5909 — Scrub by Mouse Drag Mishandles Next Video: Root Cause Report

## ROOT CAUSE

**Seek-side enabler** — `iina/PlayerCore.swift:959`  
`percent.clamped(to: 0..<100)` is insufficient: for videos with large keyframe intervals mpv rounds near-100% seeks to the nearest keyframe, which may be beyond the current file's EOF, silently advancing the playlist. The code itself carries a comment acknowledging this limitation.

**Post-seek cascade trigger** — `iina/PlayerWindowController.swift:630` + `iina/PlayerCore.swift:2578`  
`updatePlayTime` writes `playSlider.doubleValue = percentage` unconditionally (no guard for active tracking), and `syncUITimer` — which periodically calls `updatePlayTime` — is scheduled with `RunLoop.main.add(timer, forMode: .common)` (`iina/Extensions.swift:993`), meaning it fires in `NSEventTrackingRunLoopMode` during an ongoing slider drag. After each file advance the timer resets the slider position to ~0% (the new file's start); the ongoing drag immediately recomputes ~99% from mouse position and re-fires `playSliderChanges` against the newly-loaded file.

---

## CONTROL FLOW

1. **User drags the play slider knob rightward to ~99% of the track.**  
   AppKit's slider tracking loop fires `PlaySliderCell` mouse-tracking callbacks for each `NSEvent.mouseDragged`.

2. **`playSliderChanges(_:)` fires** (`iina/MainWindowController.swift:3246`) on every drag event.  
   Guard: `player.info.state.active && state != .loading` — this allows `.starting` (file in transition) and `.loaded/.playing/.paused` to proceed.  
   Calls `super.playSliderChanges(sender)` → `PlayerWindowController.playSliderChanges` (`iina/PlayerWindowController.swift:689`).

3. **`PlayerCore.seek(percent:forceExact:)` fires** (`iina/PlayerCore.swift:952`).  
   Clamps percentage to `0..<100` (exclusive), so ~99.x% passes through.  
   Issues `mpv.command(.seek, args: ["99.x", "absolute-percent+exact"], checkError: false)` — asynchronous, fire-and-forget.

4. **mpv processes the seek command** (background thread).  
   For a file with a large keyframe interval, seeking to 99.x% resolves to the nearest keyframe, which may be the very last frame or the boundary into the next file. mpv advances the playlist automatically.

5. **mpv emits `MPV_EVENT_START_FILE` for file N+1.**  
   `MPVController.handleEvent` (`iina/MPVController.swift:1139`) dispatches to the main queue:  
   `player.info.state = .starting` + `player.fileStarted(path:)`.

6. **mpv later emits `MPV_EVENT_FILE_LOADED`.**  
   `player.fileLoaded()` sets `state = .loaded`, updates `info.videoDuration`, `info.videoPosition` (now ~0s for the new file), and calls `refreshSyncUITimer()`.

7. **`refreshSyncUITimer()` starts / keeps running the `syncUITimer`** (`iina/PlayerCore.swift:2578`).  
   The timer is scheduled with `RunLoop.main.add(timer, forMode: .common)` (`iina/Extensions.swift:993`), which includes `NSEventTrackingRunLoopMode`. The timer therefore fires **during the ongoing slider drag**.

8. **Timer tick: `syncUITime()` → `syncUI(.time)` → `updatePlayTime(withDuration:andProgressBar:true)`** (`iina/PlayerWindowController.swift:606`).  
   The function passes `player.info.state.loaded` (state is now `.loaded`) and writes:  
   ```swift
   playSlider.doubleValue = percentage   // line 630; percentage ≈ 0 for new file at position 0
   ```  
   No check for `playSlider.isHighlighted` (i.e., no check for active drag). Slider knob snaps visually to ~0%.

9. **Next drag event: AppKit recomputes slider value from unchanged mouse position (~99% of track).**  
   Value changed 0% → ~99% → slider fires its continuous action → `playSliderChanges` fires again.  
   `player.info.state == .loaded` passes all guards.

10. **`seek(percent: ~99)` fires against file N+1** (step 3 repeated) → mpv advances to file N+2.

11. **Steps 5–10 repeat** for each subsequent file until the user releases the mouse or moves it away from the far-right position.

---

## WHY IT CASCADES

There are two compounding cascade mechanisms that together explain multi-file advancement.

**Mechanism A — Multiple queued seeks (operates before any GCD block runs):**  
The user's continuous drag generates multiple `NSEvent.mouseDragged` events in quick succession. Each event fires `playSliderChanges`, which synchronously calls `player.seek(percent: ~99%)`, which issues an asynchronous `mpv.command(.seek, …)`. Because `mpv.command` is fire-and-forget and `playSliderChanges` has no rate-limiting or debouncing, several seek commands can be queued in mpv's command pipeline before mpv finishes processing the first one. mpv processes them sequentially: seek 1 advances playlist from file 1 to file 2; seek 2 (now applied to file 2) advances to file 3; etc. This mechanism alone is sufficient to advance through multiple files within a single drag gesture.

**Mechanism B — Timer-reset + drag-event re-fire (operates after the file transition):**  
After mpv has advanced and the new file has loaded, `syncUITimer` (scheduled in `RunLoop.common` mode so it fires during `NSEventTrackingRunLoopMode`) calls `updatePlayTime`, which unconditionally writes `playSlider.doubleValue ≈ 0%`. AppKit's slider tracking loop then processes the next `mouseDragged` event, recomputes the slider value from the mouse position (~99%), observes the value changed from 0% to ~99%, and fires `playSliderChanges` against the new file. This re-seeding of the seek cycle means the cascade persists as long as the user holds the mouse at the far-right — even after all pre-queued seeks have been exhausted.

The two mechanisms are independent loops. Together they ensure that a drag held at the far-right position advances through all remaining playlist items.

**FALSIFICATION TEST:**  
My hypothesis for the post-seek cascade trigger (Mechanism B) is that fixing `updatePlayTime` to check `playSlider.isHighlighted` before writing would break the re-fire loop. The alternative I must consider: "If I applied the `isHighlighted` guard to `updatePlayTime` but left everything else unchanged, would the cascade still occur?"

Answer: **Yes, via Mechanism A.** Rapid drag events fire multiple seek commands before mpv processes any file transition. No fix to `updatePlayTime` affects the mpv command queue or the rate at which `playSliderChanges` fires. This is directly confirmed by inspecting `PlayerWindowController.playSliderChanges` (`iina/PlayerWindowController.swift:689–693`): it has no debounce or throttle, and `PlayerCore.seek[0]` (`iina/PlayerCore.swift:952–964`) issues each seek as a raw asynchronous mpv command. Therefore the `updatePlayTime` write is **not** the sole cascade trigger. A complete fix requires also addressing the seek side.

I also considered whether `handlePropertyChange` in `MPVController` (`iina/MPVController.swift:1252–1560`) contains a handler for `time-pos` or `percent-pos` that could independently reset the slider and re-trigger seeks. Inspection of the full switch statement shows no case for `time-pos`, `percent-pos`, or any other time-position property. Slider updates arrive only via `syncUI(.time)` → `updatePlayTime`, ruling out `handlePropertyChange` as an alternative cascade trigger.

---

## SUGGESTED FIX

The fix must address both mechanisms. **On the cascade-trigger side (Mechanism B):** in `PlayerWindowController.updatePlayTime` (`iina/PlayerWindowController.swift:629–631`), wrap the slider write in a guard that checks whether the slider is currently being tracked:

```swift
if andProgressBar && !playSlider.isHighlighted {
    let percentage = (pos.second / duration.second) * 100
    playSlider.doubleValue = percentage
    player.touchBarSupport.touchBarPlaySlider?.setDoubleValueSafely(percentage)
}
```

`NSSlider.isHighlighted` (which delegates to `PlaySliderCell.isHighlighted`) is `true` during active tracking and `false` otherwise — as the cell itself uses it to switch knob drawing colors. This prevents `syncUITimer` and `MPV_EVENT_PLAYBACK_RESTART` from resetting the slider mid-drag, breaking the re-fire loop of Mechanism B.

**On the seek side (Mechanism A):** the tightest safe fix is in `PlaySliderCell` — override `continueTracking(last:current:in:)` to return `false` (stopping the tracking session) the moment the mpv `playlist-pos` property differs from the value recorded in `startTracking`. This immediately aborts dragging when any file advance is detected, preventing further seeks from being queued. If a property read from the tracking callback is undesirable, an acceptable approximation is to tighten the upper-bound clamp in `PlayerCore.seek(percent:forceExact:)` (`iina/PlayerCore.swift:959`) from `0..<100` to `0..<99.0`, providing a wider keyframe-rounding margin — with the known trade-off that the last 1% of any file becomes unreachable via the slider. The `continueTracking` approach is cleaner because it terminates the cascade at its structural root (the ongoing drag) rather than trying to outguess keyframe positions.

---

## KEY SYMBOL REFERENCES

| Symbol | File:Lines | Role |
|---|---|---|
| `PlayerWindowController.playSliderChanges` | `iina/PlayerWindowController.swift:689–693` | Seek-firing IBAction (base) |
| `MainWindowController.playSliderChanges` | `iina/MainWindowController.swift:3246–3262` | Seek-firing IBAction (override); state guard allows `.starting` |
| `PlayerCore.seek(percent:forceExact:)` | `iina/PlayerCore.swift:952–964` | Issues async seek; clamp at line 959 |
| `PlayerWindowController.updatePlayTime` | `iina/PlayerWindowController.swift:606–633` | Unconditional slider write at line 630 |
| `PlayerCore.refreshSyncUITimer` | `iina/PlayerCore.swift:2515–2583` | Schedules timer at line 2578 |
| `Timer.scheduledTimerInCommonMode` | `iina/Extensions.swift:985–995` | Adds timer to `.common` mode (line 993), fires during drag |
| `MPVController.handleEvent` | `iina/MPVController.swift:1095–1248` | PLAYBACK_RESTART calls `syncUI(.time)` at line 1183 |
| `PlaySliderCell.startTracking` | `iina/PlaySliderCell.swift` | Pauses playback on drag start; no playlist-pos guard |
| `PlayerState.active` | `iina/PlayerState.swift:73` | `rawValue < stopping.rawValue` — `.starting` passes |
