# Transient feedback: pause bar, flash icons, wheel volume

## 1. Bar stays up while paused
- Connect `engine.state_changed` in `MainWindow`.
- playing  -> `overlay.hold(False)` + `wake()` (idle timer runs again)
- paused / ended -> `overlay.hold(True)` + `wake()` (strip pinned open)
- idle with no media: unchanged (hover-only).

## 2. Center flash icon (play / pause / mute)
New file `src/reel/flash.py`, class `Flash(QWidget)`:
- flags `Qt.Window | FramelessWindowHint | WindowDoesNotAcceptFocus |
  WindowTransparentForInput | NoDropShadowWindowHint`, attrs
  `WA_TranslucentBackground + WA_ShowWithoutActivating` -> clicks pass through.
- paints a dark disc `rgba(0,0,0,0.55)` + white icon from `widgets/icons.py`
  (`play`, `pause`, `volume_mute`, `volume_low`, `volume_high`).
- timeline: fade in 90 ms, hold 500 ms, fade out 180 ms (~0.77 s total).
- position: centered on the host client rect, icon 88 px (volume 72 px).
- API: `show_icon(name)`, `sync_geometry()`, geometry-follows-host like Overlay.

## 3. Wheel anywhere over video = volume, with its own indicator
- `VideoSurface.wheel(delta)` signal from `wheelEvent`.
- `MainWindow._on_wheel`: accumulate `delta` (touchpads send fractions),
  every 120 units = one notch = ±5 volume, clamped 0..100.
- Shows a FLASH PILL (not the bottom bar): icon + `"65%"` text, ~140x44,
  centered, same fade timings. `Flash.show_pill(icon_name, text)`.
- Volume bar in the strip is unaware; its own state syncs via `volume_changed`.

## Also
- Fix the now-wrong comment in `src/reel/dropfiles.py`: a drop on the picture
  while playing WAS verified to work, so the OLE `IDropTarget` note is wrong
  and must be deleted.
