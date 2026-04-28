# Issue #5909 — "Scrub by Mouse Drag Mishandles Next Video"

## ROOT CAUSE

`iina/PlayerCore.swift:952-964` (the `seek(percent:)` overload) — the only guard against mpv's EOF auto-advance is a `percent.clamped(to: 0..<100)` (line 959), which the in-source comment itself admits "still won't work for videos with large keyframe interval"; combined with the fact that the play-slider drag re-fires `playSliderChanges` on every mouse tick, every tick after the first rollover issues another near-100% seek against the *new* current file, which mpv treats as EOF and rolls over again.

## CONTROL FLOW

1. User presses mouse down on the play slider. `PlaySliderCell.startTracking` (`iina/PlaySliderCell.swift:171-179`) pauses the player and lets AppKit's standard NSSliderCell tracking loop take ownership of the slider's `doubleValue`.
2. The user drags toward the right edge. AppKit ties `slider.doubleValue` to the mouse X position; because `PlaySliderCell.awakeFromNib` (`iina/PlaySliderCell.swift:39-42`) sets `minValue = 0; maxValue = 100`, the value saturates at exactly `100` once the mouse passes the right edge.
3. AppKit fires the `@IBAction` target on every drag tick → `PlayerWindowController.playSliderChanges` (`iina/PlayerWindowController.swift:689-693`):
   - `percentage = 100 * sender.doubleValue / sender.maxValue` ⇒ `percentage = 100` at the saturated edge.
   - `player.seek(percent: 100, forceExact: !followGlobalSeekTypeWhenAdjustSlider)`.
4. `PlayerCore.seek(percent:)` (`iina/PlayerCore.swift:952-964`) clamps `100` through the `FloatingPoint.clamped(to: Range)` extension at `iina/Extensions.swift:413-421`, which returns `range.upperBound.nextDown` for `self >= upperBound` — so the value passed to mpv is `100.nextDown ≈ 99.99999999999999`.
5. mpv runs an `absolute-percent` (or `absolute-percent+exact`) seek. Because the clamped value is one ULP below 100%, the seek can land at or past the file's last decodable frame. mpv interprets reaching EOF on a playlist-backed source as "this file ended" and auto-advances to the next playlist entry (the comment at `PlayerCore.swift:954-957` documents this limitation).
6. mpv emits `MPV_EVENT_END_FILE` → `MPV_EVENT_START_FILE` (sets `info.state = .starting`) → `MPV_EVENT_FILE_LOADED` → `PlayerCore.fileLoaded()` (`iina/PlayerCore.swift:2087-2154`), which sets `info.state = .loaded` and overwrites `info.videoDuration` / `info.videoPosition` for the new file.
7. The drag is still in progress: AppKit's tracking loop is still pumping mouse-dragged events. The user's mouse has barely moved (still near the right edge), so the slider's `doubleValue` is still `~100`. The cell does not get a fresh `updatePlayTime`-driven reset until syncUI fires, and even when it does, the next mouse-dragged tick yanks `doubleValue` back to the mouse position.
8. The next drag tick fires `playSliderChanges` again. `PlayerWindowController`'s guard is just `guard player.info.state.active`; `MainWindowController`'s override (`iina/MainWindowController.swift:3246-3262`) adds `state != .loading`. By the time the drag tick lands, the state has already advanced from `.loading`/`.starting` to `.loaded`, so the guards pass.
9. `seek(percent: ~100)` fires again — this time targeting the *new* current file. Because the percent is again clamped to `100.nextDown`, the seek can again land at EOF, and mpv auto-advances to file 3.
10. Steps 7–9 repeat for as long as the user continues to hold the mouse near the right edge. The visible result is the playlist cascading.

## WHY IT CASCADES

Two structural facts combine to produce the cascade. First, the EOF guard lives in *percent space*: clamping `100` to `100.nextDown` produces a value that is mathematically below 100 but, on any container where the last decodable PTS is even slightly before the declared duration (which is normal — large keyframe intervals, demuxer rounding, container timestamp granularity), still translates to "past the last frame", and mpv reacts to that exactly the way it reacts to natural EOF on a playlist source: by advancing. Second, nothing in either `playSliderChanges` or `seek(percent:)` recognises that a drag is ongoing — there is no per-drag flag set by `PlaySliderCell.startTracking`/`stopTracking` and no suppression of seeks while a file change initiated by an *earlier* slider tick is still in flight. So once mpv has rolled over once, every subsequent mouse-tick during the same drag is a free, fully-armed near-100% seek against whatever file has now become current, and each such seek can re-trigger the same rollover. The user perceives this as "the drag delta is being applied across files" because every tick of mouse movement at the slider's right edge translates one-for-one into another playlist advance — but mechanically, each of those advances is its own independent EOF event, not a cumulative delta.

## SUGGESTED FIX

The cleanest minimal fix is to stop seeking by percent at all in `seek(percent:)` when the duration is known — convert to seconds and clamp in time space, where mpv's "past EOF" interpretation can be avoided deterministically. Replace the body of the `if let duration = info.videoDuration?.second, duration > 0` block at `PlayerCore.swift:957-960` with something equivalent to: `let target = (percent * 0.01 * duration).clamped(to: 0...(duration - 0.1))` and dispatch the seek as `mpv.command(.seek, args: ["\(target)", useExact ? "absolute+exact" : "absolute"], ...)` — i.e. a one-or-two-line conversion to absolute-second seeking with a small explicit pre-EOF buffer. A complementary per-drag guard in `PlaySliderCell.startTracking`/`stopTracking` (set an `isScrubbing` flag and have `playSliderChanges` short-circuit further seeks once the *first* of them caused an end-of-file transition during the same drag) would prevent the secondary rollovers even on files where the time-space clamp turns out to be too close. Either change keeps the user's expected single-file rollover-at-end behaviour while eliminating the cascade.

DONE

