# IINA #5909 — Scrub by Mouse Drag Mishandles Next Video

## ROOT CAUSE

`iina/PlayerCore.swift:957-960` (the `seek(percent:)` clamp) — The clamp
`percent = percent.clamped(to: 0..<100)` resolves to `100.nextDown` (the
`FloatingPoint.clamped(to: Range<Self>)` extension at
`iina/Extensions.swift:412-420` returns `range.upperBound.nextDown` for
out-of-range values). For `Double`, that is `≈ 99.99999999999999`, which
maps to a sub-femtosecond gap before EOF for any real-world file
duration; mpv treats the resulting `absolute-percent+exact` seek as
reaching EOF and auto-advances the playlist. The "drag past end"
cascade is then driven by the slider IBAction firing repeatedly as each
file change resets the slider value while the user's mouse is still
pegged at the right edge.

## CONTROL FLOW

1. User starts dragging the play scrubber.
   `PlaySliderCell.startTracking` (`iina/PlaySliderCell.swift:174-182`)
   calls `playerCore.pause()` and lets `super.startTracking` run, so
   subsequent mouse events drive `NSSliderCell`'s standard tracking
   loop. `PlaySliderCell.awakeFromNib` (`PlaySliderCell.swift:39-42`)
   has set `minValue=0`, `maxValue=100`.
2. User drags past the slider's right edge. `NSSliderCell` clamps
   `playSlider.doubleValue` to `maxValue = 100` and fires the
   `playSliderChanges` IBAction.
3. `PlayerWindowController.playSliderChanges`
   (`iina/PlayerWindowController.swift:688-692`) computes
   `percentage = 100 * sender.doubleValue / sender.maxValue = 100` and
   calls `player.seek(percent: 100, forceExact: !followGlobalSeekTypeWhenAdjustSlider)`.
   With the default preference (`followGlobalSeekTypeWhenAdjustSlider:
   false`, `iina/Preference.swift:1001`), `forceExact = true`.
4. `PlayerCore.seek(percent:)` (`iina/PlayerCore.swift:951-963`):
   `info.videoDuration` is non-nil and positive, so the clamp runs:
   `percent = 100.0.clamped(to: 0..<100) = 100.0.nextDown ≈
   99.99999999999999`. Because `forceExact == true`, `useExact = true`
   and the seek is issued as
   `mpv.command(.seek, args: ["99.99999999999999", "absolute-percent+exact"])`.
5. mpv computes the target time `≈ duration * 99.99999999999999 / 100`,
   which rounds to `duration` at any normal time resolution. mpv treats
   that as EOF, emits `MPV_EVENT_END_FILE`, and (with the default "play
   next item automatically" behavior) auto-advances to the next
   playlist entry. `MPVController.handleEvent`
   (`iina/MPVController.swift:1186-1204`) only force-pauses on EOF when
   the `pauseWhenOpen` preference is set, so by default the new file
   starts loading and playing.
6. `MPV_EVENT_FILE_LOADED` triggers `PlayerCore.fileLoaded`
   (`iina/PlayerCore.swift:2110`), which sets `info.videoDuration` to
   the new file's duration. `updatePlayTime`
   (`iina/PlayerWindowController.swift:605-632`) then writes
   `playSlider.doubleValue = (pos.second / duration.second) * 100`,
   i.e. ≈ 0 for the freshly-started file.
7. The user's mouse is still pressed and is still past the slider's
   right edge. On the next mouse-tracking tick, `NSSliderCell` recomputes
   `doubleValue` from the cursor position and snaps it back to `100`.
   That is a value change, so `playSliderChanges` fires again.
8. Loop to step 3, now operating on file 2. Each iteration of the loop
   advances exactly one playlist entry. The cascade continues for as
   long as the user keeps dragging past the right edge.

## WHY IT CASCADES

The author of `seek(percent:)` knew about the EOF auto-advance hazard
(see the comment immediately above the clamp: "mpv will play next file
automatically when seek to EOF. We clamp to a Range to ensure that we
don't try to seek to 100%"), but the chosen mechanism — a half-open
`Range<Double>` ending at `100`, resolved by an extension that returns
`upperBound.nextDown` — only retreats by one floating-point ULP. One
ULP at magnitude 100 in `Double` is on the order of `10⁻¹⁴`; converted
back into seconds it is sub-femtosecond, i.e. far below mpv's actual
playback time resolution. mpv therefore can't distinguish that target
from "duration", treats the seek as "reached EOF", and runs its normal
end-of-file playlist-advance path. The follow-on comment "however, it
still won't work for videos with large keyframe interval" captures the
*non-exact* failure mode, but the exact failure mode (insufficient
absolute-time margin even when `+exact` is in effect) was missed.
Cascading then arises naturally because the drag interaction is a tight
loop: each successful auto-advance loads a new file, the position-sync
path snaps the slider back toward 0, the user's still-held cursor
immediately drives it back to 100, and the IBAction re-fires — there
is no per-drag gate that says "this gesture has already produced a
playlist advance, ignore further saturated values."

## SUGGESTED FIX

Replace the percent-domain clamp in `PlayerCore.seek(percent:)` with a
duration-aware absolute-time clamp that leaves a meaningful margin
before EOF — e.g. compute `let safe = max(0, duration - 1.0)` (or
similar; even a few hundred milliseconds is enough for mpv) and then
`percent = percent.clamped(to: 0...(safe / duration * 100))`. That
keeps the seek visibly inside the current file at any normal time
resolution, so the EOF auto-advance never fires and the drag stays
clamped to the current item exactly as the bug report describes as
expected. As a defense-in-depth follow-up, `playSliderChanges` could
also short-circuit when `sender.doubleValue` is unchanged from the
previously-issued seek target, so that even if mpv ever does advance,
the IBAction wouldn't immediately re-arm the cascade after the slider
is reset by `updatePlayTime`. The primary fix is the duration-aware
clamp; the IBAction guard is a secondary safety net.
