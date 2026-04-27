# Issue #5909 — Scrub by Mouse Drag Mishandles Next Video

## ROOT CAUSE
`iina/PlayerCore.swift:952-963` (`PlayerCore.seek(percent:forceExact:)`) — the
"don't let the slider seek to EOF" safeguard there is a sub-100% percent
clamp that, combined with mpv's non-exact percent seek snapping to the file's
last keyframe, does not actually keep each drag-driven seek strictly inside
the current file; mpv therefore reaches end-of-file and auto-advances to the
next playlist entry on every near-100% slider event, and because NSSlider's
mouse-tracking session continues across the file change, the same mouse drag
keeps producing near-100% events and the player cascades.

## CONTROL FLOW
1. User grabs the slider thumb. `PlaySliderCell.startTracking` (`iina/PlaySliderCell.swift:167-175`) records `isPausedBeforeSeeking`, calls `playerCore.pause()`, hides the thumbnail peek view, and returns to AppKit's continuous mouse-tracking loop. No upper-bound or "you are at EOF" state is established.
2. The user drags right. NSSlider's tracking reads the cursor's x-position relative to the bar and clamps the resulting `doubleValue` into `[minValue, maxValue]` = `[0, 100]` (set in `PlaySliderCell.awakeFromNib`, `iina/PlaySliderCell.swift:39-42`). When the cursor is at or past the right edge, `doubleValue` saturates at 100. NSSlider fires the `@IBAction playSliderChanges:` selector.
3. `PlayerWindowController.playSliderChanges` (`iina/PlayerWindowController.swift:688-692`) — and its override `MainWindowController.playSliderChanges` (`iina/MainWindowController.swift:3245-3261`) — compute `percentage = 100 * sender.doubleValue / sender.maxValue` (so up to 100.0) and call `player.seek(percent: percentage, forceExact: !followGlobalSeekTypeWhenAdjustSlider)`.
4. `PlayerCore.seek(percent:forceExact:)` (`iina/PlayerCore.swift:952-963`) clamps:
   ```swift
   if let duration = info.videoDuration?.second, duration > 0 {
     percent = percent.clamped(to: 0..<100)
   }
   ```
   The half-open `Range` clamp goes through `FloatingPoint.clamped(to:)` (`iina/Extensions.swift:412-420`), which returns `range.upperBound.nextDown` when `self >= upperBound` — i.e. ~`99.99999…`. The seek mode is `"absolute-percent+exact"` if `forceExact || Preference.bool(for: .useExactSeek)` else `"absolute-percent"` (non-exact). For a user with default prefs (`followGlobalSeekTypeWhenAdjustSlider = true`, `useExactSeek = false`), the call goes out as plain `absolute-percent`.
5. With `absolute-percent` (no `+exact`), mpv resolves the position to the nearest keyframe. On files with a large keyframe interval, the keyframe at or beyond the requested percentage can effectively be EOF — exactly what the comment in `seek(percent:)` warns about: *"however, it still won't work for videos with large keyframe interval"*. mpv treats the seek as having reached end-of-file and, per default mpv playlist behaviour, advances to the next entry.
6. The new file loads. `PlayerCore.fileStarted` (`iina/PlayerCore.swift:1999-2078`) updates `info.currentURL` etc. but does **not** clear `info.videoDuration`. `PlayerCore.fileLoaded` (`iina/PlayerCore.swift:2086-2153`) then writes the new duration/position into `info`, force-redraws the window, and calls `refreshSyncUITimer()` / `syncUI(.playlist)`. The play-time sync (`syncUI(.time)` in `iina/PlayerCore.swift:2662-2727`) drives the slider's `doubleValue` back down to ~0 to reflect the new file's `videoPosition` of 0.
7. The user has not lifted the mouse: `PlaySliderCell.stopTracking` (`iina/PlaySliderCell.swift:177-182`) only fires on mouse-up. NSSlider's tracking loop is still active and still reading cursor-x. The next `mouseDragged` updates `doubleValue` from the mouse position, which is still pinned at the slider's right edge → `doubleValue` snaps from ~0 right back to 100, and NSSlider fires another `playSliderChanges:`.
8. Goto step 3. Each iteration produces another near-100% seek, another EOF, another playlist advance. The cascade is one EOF per AppKit tracking tick for as long as the cursor stays past the right edge of the slider.

## WHY IT CASCADES
Two independent design assumptions break at the same time. The first is in `seek(percent:)`: the author knew "a 100% seek will roll mpv onto the next file" (that is literally the comment in the function), so they tried to avoid it with `percent.clamped(to: 0..<100)`. The clamp does its arithmetic job — the value sent is `99.999…` — but it does **not** prevent mpv's non-exact percent seek from snapping forward to a terminal keyframe, and the comment itself flags this caveat without mitigating it. The second assumption is on the slider side: `PlaySliderCell.startTracking`/`stopTracking` only manage pause/resume and the thumbnail peek view; nothing observes file boundaries, nothing calls `abortTracking`, and nothing makes the `playSliderChanges:` IBAction reject a slider event that comes in for a different file than the one tracking started on. So as soon as step 5 advances the playlist, `syncUI(.time)` resets the slider to 0 and the still-live tracking session immediately re-derives ~100 from the unchanged cursor x, re-entering `seek(percent:)` against the **new** file, which mpv promptly advances out of by the same mechanism. Each cascade hop is one full round-trip of (drag tick → seek to 99.99% → mpv keyframe → EOF → next file → slider sync → drag tick re-saturates at 100). The "delta being applied across files" the bug description suspects is really the same near-100% percent being re-applied to whichever file mpv is currently sitting on.

## SUGGESTED FIX
The narrowest, surest fix is in `PlayerCore.seek(percent:forceExact:)` at `iina/PlayerCore.swift:952-963`: replace the percent-domain clamp with a duration-domain (seconds) clamp and route through `seek(absoluteSecond:)`, e.g. when `info.videoDuration?.second` is known and positive, compute `target = min(percent / 100.0 * duration, duration - 1.0)` and call `seek(absoluteSecond: target)` (which already uses `absolute+exact`); fall back to the existing percent path only when duration is genuinely unknown, in which case the clamp should still be applied unconditionally (i.e. lift `percent.clamped(to: 0..<100)` out of the `if let duration` block). This guarantees mpv is never asked to seek to a position whose nearest keyframe is at or beyond EOF, so the playlist auto-advance never fires from a slider drag and the cascade is impossible — regardless of whether the user has exact seek enabled or how large the file's keyframe interval is. (A defensible alternative one-liner is to force `forceExact = true` for the slider drag path, which removes the keyframe-snap risk in the percent domain; the seconds-with-margin approach is preferable because it does not depend on mpv's exact-seek being available/cheap for the underlying codec.)

DONE
