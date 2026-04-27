# IINA #5909 — Scrub by Mouse Drag Mishandles Next Video

## ROOT CAUSE
`iina/PlayerCore.swift:951-963` — the percent-clamp in `PlayerCore.seek(percent:forceExact:)` (`percent = percent.clamped(to: 0..<100)`) returns `100.nextDown` (≈ 99.99999999999999), a value that is *not* far enough below 100% to keep the resulting target time inside every video's decodable range; mpv still emits `MPV_EVENT_END_FILE` for the seek, the playlist auto-advances, and the user's continued drag at the slider's right edge re-fires the same seek on each successive file. The existing comment at line 956 ("however, it still won't work for videos with large keyframe interval") explicitly concedes the clamp is incomplete.

## CONTROL FLOW
1. User presses the slider knob → `PlaySliderCell.startTracking` (`iina/PlaySliderCell.swift:174`) calls `playerCore.pause()`. The player is now paused for the duration of the drag.
2. Each `mouseDragged` event is dispatched into `NSSliderCell`'s tracking loop, which sets `slider.doubleValue` to the mouse-position percentage clamped to `[minValue, maxValue] = [0, 100]` and fires the action target.
3. The action lands in `MainWindowController.playSliderChanges` (`iina/MainWindowController.swift:3245-3261`), whose only guard is `state.active && state != .loading`. Both pass during a drag, so it forwards to `super.playSliderChanges`.
4. `PlayerWindowController.playSliderChanges` (`iina/PlayerWindowController.swift:688-692`) computes `percentage = 100 * sender.doubleValue / sender.maxValue` (which equals 100 whenever the mouse is at/past the right edge) and calls `player.seek(percent: 100, forceExact: !followGlobalSeekTypeWhenAdjustSlider)`. With the default `followGlobalSeekTypeWhenAdjustSlider = false` (`iina/Preference.swift:1001`), `forceExact == true`.
5. `PlayerCore.seek(percent:forceExact:)` (`iina/PlayerCore.swift:951-963`) reduces 100 to `100.nextDown` via `clamped(to: 0..<100)` (the FloatingPoint clamp at `iina/Extensions.swift:412-420` returns `range.upperBound.nextDown` for `self >= upperBound`) and runs `mpv.command(.seek, args: ["99.999…", "absolute-percent+exact"])`.
6. For many real-world files (where mpv's `duration` property overshoots the last decodable frame, or keyframe layout pushes the exact-decode point past EOF), mpv emits `MPV_EVENT_END_FILE`. `MPVController.handleEvent` (`iina/MPVController.swift:1186-1204`) marshals this to `PlayerCore.fileEnded(_:)` (`iina/PlayerCore.swift:2155`).
7. With IINA's default mpv config — `keepOpenOnFileEnd = true` *and* `playlistAutoPlayNext = true` — the `keep-open` option is set to `"yes"` (`iina/MPVController.swift:381-393`). Per mpv, `keep-open=yes` only blocks termination at the *last* playlist entry; middle entries auto-advance. So mpv loads the next file.
8. `PlayerCore.fileLoaded()` (`iina/PlayerCore.swift:2086-2153`) updates `info.videoDuration` to the new file's duration and `PlayerWindowController.updatePlayTime` (`iina/PlayerWindowController.swift:605-632`) writes `playSlider.doubleValue = 0` for the new file.
9. The user has not released the mouse; it is still at the slider's right edge. NSSliderCell's tracking loop continues. The next `mouseDragged` re-pins `slider.doubleValue` to ~100 (mouse-position-driven, not playback-driven), the action fires again, and steps 4–8 repeat against the now-current file. The cascade is bounded only by playlist length.

## WHY IT CASCADES
The bug report's own phrase — "as if the drag delta were being applied across files rather than clamped to the current one" — is exactly correct: the slider's `doubleValue` is purely a function of mouse-x during NSSliderCell tracking, with no awareness of which file the playback engine is currently on. The author of `seek(percent:)` understood the basic problem (mpv auto-advances on EOF-percent seeks) and added the `0..<100` clamp as protection, but `Range.upperBound.nextDown` of `100.0` is `99.99999999999999`, so the actual mpv target is `0.9999999999999999 * duration`, ≈ `duration − 6e-15·duration`. For any file whose decodable range falls even a few hundred microseconds short of `duration` (very common — duration metadata, last-frame-display-end vs last-frame-display-start, large keyframe intervals all cause this), the seek lands past the last frame and mpv emits EOF. Once an EOF advance occurs, `PlaySliderCell.startTracking`'s pause does nothing to stop the next round: the user is still mouse-down, the cell's tracking loop re-issues `continueTracking`, the slider re-pins to 100, `playSliderChanges` re-fires, and the next file gets the same near-EOF seek treatment. There is no "is-scrubbing" gate around the seek path and no comparable gate around the playlist's response to in-drag EOF events; the only guard (`isPausedBeforeSeeking`/`startTracking`) just controls the resume-after-drop behavior. (Notably, the touch-bar slider has `TouchBarPlaySlider.setDoubleValueSafely` which guards on `isTouching` — `iina/TouchBarSupport.swift:290-293` — to avoid the symmetric "playback updates clobber user-drag" problem, but no analogous gate exists here on the *seek* side.)

## SUGGESTED FIX
Make the clamp operate in seconds with a real safety margin instead of in percent. In `PlayerCore.seek(percent:forceExact:)` (`iina/PlayerCore.swift:951-963`), when a non-zero duration is known, convert percent to a target time, clamp it to `[0, duration − ε]` for an `ε` large enough to dwarf metadata/keyframe slop (e.g., 1.0 second, or a small fraction of duration whichever is larger), and issue `absolute+exact` rather than `absolute-percent+exact`. Sketch:

```swift
if let duration = info.videoDuration?.second, duration > 0 {
    let target = (percent / 100) * duration
    let safe = min(max(target, 0), max(0, duration - 1.0))
    let mode = useExact ? "absolute+exact" : "absolute"
    mpv.command(.seek, args: ["\(safe)", mode], checkError: false, level: .verbose)
    return
}
```

This guarantees a slider-driven seek can never land at or past the last decodable frame regardless of duration-metadata accuracy or keyframe interval, so mpv never emits EOF mid-drag and the playlist never auto-advances during scrubbing. As a complementary in-depth safeguard, `PlayerWindowController.playSliderChanges` (`iina/PlayerWindowController.swift:688-692`) could short-circuit (`guard sender.doubleValue < sender.maxValue else { return }`) so a stationary mouse pinned at the right edge does not keep re-firing the action even once the seek-side fix is in place.

DONE
