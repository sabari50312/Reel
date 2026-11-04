"""A zoomed-in strip for syncing subtitles.

Deliberately not a general "zoom" on the seek bar: zoom alone is a knob with no
job. This strip exists for one task - lining captions up with speech - so it
shows exactly the two signals that task needs, magnified:

    cue lane     where the subtitle file thinks each line sits (shifted by the
                 current subtitle delay, so nudging Z/X visibly slides them)
    sound lane   where the file actually has sound, from the loudness map

No ticks, no grid, no extra chrome: only the two lanes, a playhead and the
window's start/end times. Wheel changes the window (10/20/40/60 s); dragging
anywhere in it scrubs, and the window freezes while you drag so the picture
cannot slide out from under the cursor.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPaintEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ..style import tokens
from .timefmt import format_time, needs_hours

STRIP_H = 64
_WINDOW_CHOICES = (10_000, 20_000, 40_000, 60_000)
_LANE_H = 18.0
_CUE_TOP = 6.0
_SOUND_TOP = 30.0
_LABEL_TOP = 50.0
_RAIL_RADIUS = 3.0
_PLAYHEAD_W = 2.0
_CUE_ALPHA = 0.55  # white: captions are a different signal from the green loudness
_SOUND_ALPHA = 0.55
_GAP_BUCKETS = 2


class SyncStrip(QWidget):
    seek_requested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(STRIP_H)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Drag to scrub; wheel changes the window")

        self._duration = 0
        self._position = 0
        self._levels: list[int] = []
        self._cues: list[tuple[int, int]] = []
        self._delay = 0
        self._window = 20_000
        self._dragging = False
        self._frozen_start = 0
        self._bucket_ms = 250  # matches audiomap.BUCKET_S

    # ------------------------------------------------------------------ input
    def set_duration(self, milliseconds: int) -> None:
        self._duration = max(0, int(milliseconds))
        self.update()

    def set_position(self, milliseconds: int) -> None:
        self._position = max(0, int(milliseconds))
        self.update()

    def set_levels(self, levels: list[int] | None) -> None:
        self._levels = list(levels or [])
        self.update()

    def set_cues(self, cues: list[tuple[int, int]] | None) -> None:
        self._cues = list(cues or [])
        self.update()

    def set_delay(self, milliseconds: int) -> None:
        self._delay = int(milliseconds)
        self.update()

    def has_cues(self) -> bool:
        return bool(self._cues)

    # --------------------------------------------------------------- geometry
    def _span(self) -> tuple[int, int]:
        """Visible window in milliseconds: follows the playhead unless dragging."""
        duration = max(self._duration, self._window)
        if self._dragging:
            start = self._frozen_start
        else:
            start = self._position - self._window // 2
        start = max(0, min(start, max(0, duration - self._window)))
        return start, start + self._window

    def _x_of(self, milliseconds: float) -> float:
        start, end = self._span()
        fraction = (milliseconds - start) / max(1, end - start)
        return self.width() * fraction

    def _ms_at(self, x: float) -> int:
        start, end = self._span()
        fraction = max(0.0, min(1.0, x / max(1.0, float(self.width()))))
        return int(start + fraction * (end - start))

    # ------------------------------------------------------------------ paint
    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)

        cue_lane = QRectF(0, _CUE_TOP, self.width(), _LANE_H)
        sound_lane = QRectF(0, _SOUND_TOP, self.width(), _LANE_H)
        painter.setBrush(QColor(tokens.BLOCK_OFF))
        painter.drawRoundedRect(cue_lane, _RAIL_RADIUS, _RAIL_RADIUS)
        painter.drawRoundedRect(sound_lane, _RAIL_RADIUS, _RAIL_RADIUS)

        self._paint_cues(painter, cue_lane)
        self._paint_sound(painter, sound_lane)
        self._paint_playhead(painter, cue_lane, sound_lane)
        self._paint_labels(painter)
        painter.end()

    def _paint_cues(self, painter: QPainter, lane: QRectF) -> None:
        if not self._cues:
            # libVLC 3 exposes no cue timings for embedded tracks, so without a
            # subtitle *file* this lane has nothing to draw. Say so instead of
            # leaving a blank bar that looks broken.
            font = QFont(tokens.FONT_FAMILY)
            font.setPixelSize(tokens.FS_SMALL - 1)
            painter.setFont(font)
            painter.setPen(QPen(QColor(tokens.TEXT_FAINT)))
            painter.drawText(
                lane,
                Qt.AlignmentFlag.AlignCenter,
                "Subtitle cues need a subtitle file",
            )
            return
        tint = QColor(tokens.ACCENT)
        tint.setAlphaF(_CUE_ALPHA)
        painter.setBrush(tint)
        start, end = self._span()
        for cue_start, cue_end in self._cues:
            left = cue_start + self._delay
            right = cue_end + self._delay
            if right < start or left > end:
                continue
            x0 = max(0.0, self._x_of(left))
            x1 = min(float(self.width()), self._x_of(right))
            if x1 - x0 < 1.5:  # a very short line still gets a visible mark
                x1 = x0 + 1.5
            painter.drawRoundedRect(QRectF(x0, lane.top(), x1 - x0, lane.height()), 2.0, 2.0)

    def _paint_sound(self, painter: QPainter, lane: QRectF) -> None:
        if not self._levels:
            return
        start, _end = self._span()
        count = len(self._levels)
        first = max(0, int(start / self._bucket_ms))
        last = min(count - 1, int((start + self._window) / self._bucket_ms))

        tint = QColor(tokens.ACCENT_GREEN)
        tint.setAlphaF(_SOUND_ALPHA)
        painter.setBrush(tint)

        active = False
        run_start = 0
        quiet = 0
        for index in range(first, last + 1):
            level = self._levels[index]
            if level >= 22:
                if not active:
                    active, run_start = True, index
                quiet = 0
                continue
            if active:
                quiet += 1
                if level < 12 and quiet >= _GAP_BUCKETS:
                    self._draw_run(painter, lane, run_start, index - quiet + 1)
                    active = False
        if active:
            self._draw_run(painter, lane, run_start, last + 1)

    def _draw_run(self, painter: QPainter, lane: QRectF, first: int, last: int) -> None:
        x0 = self._x_of(first * self._bucket_ms)
        x1 = self._x_of(last * self._bucket_ms)
        if x1 - x0 < 1.0:
            return
        painter.drawRect(QRectF(x0, lane.top(), x1 - x0, lane.height()))

    def _paint_playhead(self, painter: QPainter, cue_lane: QRectF, sound_lane: QRectF) -> None:
        x = self._x_of(self._position)
        if not (0.0 <= x <= self.width()):
            return
        painter.setBrush(QColor(tokens.ACCENT_GREEN))
        painter.drawRect(
            QRectF(x - _PLAYHEAD_W / 2.0, cue_lane.top(), _PLAYHEAD_W, sound_lane.bottom() - cue_lane.top())
        )

    def _paint_labels(self, painter: QPainter) -> None:
        if self._duration <= 0:
            return
        font = QFont(tokens.FONT_MONO)
        font.setPixelSize(tokens.FS_SMALL - 1)
        painter.setFont(font)
        painter.setPen(QPen(QColor(tokens.TEXT_FAINT)))
        start, end = self._span()
        hours = needs_hours(self._duration)
        painter.drawText(
            QRectF(0, _LABEL_TOP, self.width() / 2.0, STRIP_H - _LABEL_TOP),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            format_time(start, hours),
        )
        painter.drawText(
            QRectF(self.width() / 2.0, _LABEL_TOP, self.width() / 2.0, STRIP_H - _LABEL_TOP),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            format_time(end, hours),
        )

    # ------------------------------------------------------------------ events
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            # Freeze the visible window BEFORE entering dragging mode. `_span()`
            # returns the frozen start once `_dragging` is set, so reading it
            # after flipping the flag always returned 0 and scrubbing jumped
            # back to the start of the file.
            self._frozen_start = self._span()[0]
            self._dragging = True
            self.seek_requested.emit(self._ms_at(event.position().x()))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._dragging:
            self.seek_requested.emit(self._ms_at(event.position().x()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._dragging and event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: ANN001 - QWheelEvent
        step = 1 if event.angleDelta().y() > 0 else -1
        index = _WINDOW_CHOICES.index(self._window) if self._window in _WINDOW_CHOICES else 1
        index = max(0, min(len(_WINDOW_CHOICES) - 1, index + step))
        self._window = _WINDOW_CHOICES[index]
        self.update()
        event.accept()


__all__ = ["STRIP_H", "SyncStrip"]
