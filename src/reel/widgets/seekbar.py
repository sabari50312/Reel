"""Seek bar: thin track, hover time bubble, drag-to-scrub, live seek preview.

Position never jumps while scrubbing: `set_position` is ignored for the duration
of a drag, so the 30 Hz poll cannot yank the knob out from under the cursor. The
engine is asked to seek on every drag move (VLC handles that fine) which means
the frame under the scrub really does follow the knob.

The widget is taller than the visible track on purpose: the top strip is where
the hover time bubble is painted, inside this widget's own rect, so no extra
window or tooltip is needed.
"""

from __future__ import annotations

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QEnterEvent,
    QFont,
    QLinearGradient,
    QMouseEvent,
    QPaintEvent,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QWidget

from ..style import tokens
from .timefmt import format_time, needs_hours

SEEKBAR_H = 56  # 18 px bubble above a 18 px rail that sits on the centre line
_TRACK_PAD = tokens.SEEK_KNOB / 2.0  # keeps the knob inside the widget
_RAIL_RADIUS = 4.0  # the rail itself is rounded; highlights are not
_SOUND_ALPHA = 0.24  # ahead of the playhead: sound is only a hint, so a light tint
_PLAYED_ALPHA = 0.62  # saturated but translucent, so the sound runs still show
_SOUND_ON_PLAYED = 0.22  # sound inside the played part: a light accent over the green
_PILL_W = 4.0
_PILL_OVERHANG = 0.0  # the playhead is exactly the rail height
_ON_LEVEL = 22  # amplitude that starts a "sound here" run
_OFF_LEVEL = 12  # ...and that ends one (hysteresis)
_GAP_BUCKETS = 2  # consecutive quiet buckets (0.5 s) before a run is closed


class SeekBar(QWidget):
    seek_requested = Signal(int)  # absolute position, ms
    scrubbing_changed = Signal(bool)  # keep the overlay awake while held

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(SEEKBAR_H)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._duration = 0
        self._position = 0
        self._hover = False
        self._hover_x = 0.0
        self._dragging = False
        self._grow = 0.0  # 0 = resting track, 1 = hover thickness
        self._levels: list[int] | None = None
        self._bucket_ms = 250  # width of one loudness bucket, matches audiomap  # loudness bands, 0..100 per bucket

        self._grow_anim = QPropertyAnimation(self, b"grow", self)
        self._grow_anim.setDuration(tokens.GROW_MS)
        self._grow_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    # ------------------------------------------------------------- animation
    def get_grow(self) -> float:
        return self._grow

    def set_grow(self, value: float) -> None:
        if value != self._grow:
            self._grow = value
            self.update()

    grow = Property(float, get_grow, set_grow)

    def _animate_grow(self, target: float) -> None:
        self._grow_anim.stop()
        self._grow_anim.setStartValue(self._grow)
        self._grow_anim.setEndValue(target)
        self._grow_anim.start()

    # ----------------------------------------------------------------- state
    def set_duration(self, ms: int) -> None:
        duration = max(0, int(ms))
        if duration != self._duration:
            self._duration = duration
            self.update()

    def set_position(self, ms: int) -> None:
        if self._dragging:
            return
        position = max(0, min(int(ms), self._duration or int(ms)))
        if position != self._position:
            self._position = position
            self.update()

    # --------------------------------------------------------------- helpers
    def _track_y(self) -> float:
        # Track sits low so the hover bubble gets the top strip, and clear of the
        # bottom edge by the pill's overhang: the playhead must not be clipped.
        return self.height() / 2.0  # centre of the row, not hugging the bottom edge

    def _usable_width(self) -> float:
        return max(1.0, self.width() - 2 * _TRACK_PAD)

    def _fraction_at(self, x: float) -> float:
        return min(1.0, max(0.0, (x - _TRACK_PAD) / self._usable_width()))

    def _x_for(self, fraction: float) -> float:
        return _TRACK_PAD + fraction * self._usable_width()

    def _position_for(self, x: float) -> int:
        return int(self._fraction_at(x) * self._duration)

    # ----------------------------------------------------------------- input
    def enterEvent(self, event: QEnterEvent) -> None:
        self._hover = True
        self._animate_grow(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: ANN001 - Qt signature
        self._hover = False
        if not self._dragging:
            self._animate_grow(0.0)
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._duration <= 0:
            super().mousePressEvent(event)
            return
        self._dragging = True
        self.scrubbing_changed.emit(True)
        self._hover = True
        self._hover_x = event.position().x()
        self._seek_to(self._hover_x)
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._hover_x = event.position().x()
        if self._dragging:
            self._seek_to(self._hover_x)
        else:
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            self.scrubbing_changed.emit(False)
            if not self._hover:
                self._animate_grow(0.0)
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _seek_to(self, x: float) -> None:
        position = self._position_for(x)
        self._position = position
        self.update()
        self.seek_requested.emit(position)

    # ----------------------------------------------------------------- paint
    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)

        thickness = tokens.SEEK_H + (tokens.SEEK_H_HOVER - tokens.SEEK_H) * self._grow
        y = self._track_y()
        left = _TRACK_PAD
        width = self._usable_width()
        fraction = (self._position / self._duration) if self._duration > 0 else 0.0
        fraction = min(1.0, max(0.0, fraction))
        head = left + width * fraction
        track = QRectF(left, y - thickness / 2.0, width, thickness)
        radius = _RAIL_RADIUS

        # One flat rail. Three layers, no second strip and no blocks:
        #   1. the rail itself                        (no sound, not played)
        #   2. sound runs painted across the rail     (highlight = "audio here")
        #   3. a light veil over the played part      (progress = lighter)
        painter.setBrush(QColor(tokens.BLOCK_OFF))
        painter.drawRoundedRect(track, radius, radius)

        # Sound runs are painted *after* the played band, with a lighter tint on
        # the played side: a green layer over green would hide them entirely.
        if fraction > 0.0:
            # Saturation is the progress cue, the way YouTube reads: grey rail,
            # coloured played part. Drawn as a clip of the whole rail so the
            # rounded ends still come from the rail, not from this layer.
            painter.save()
            painter.setClipRect(QRectF(left, track.top(), head - left, thickness))
            played = QColor(tokens.ACCENT_GREEN)
            played.setAlphaF(_PLAYED_ALPHA)
            painter.setBrush(played)
            painter.drawRoundedRect(track, radius, radius)
            painter.restore()

        if self._levels:
            self._paint_sound(painter, left, width, track, radius, head)

        # Playhead: a sharp 4px bar exactly as tall as the rail. A rounded pill
        # that stuck out above and below read as a separate widget.
        pill_w = _PILL_W + (1.0 if (self._hover or self._dragging) else 0.0)
        pill = QRectF(head - pill_w / 2.0, track.top(), pill_w, thickness)
        painter.setBrush(QColor(tokens.ACCENT_GREEN))
        painter.drawRect(pill)

        if self._hover and self._duration > 0:
            self._paint_bubble(painter, y, thickness)
        painter.end()

    def _paint_sound(
        self,
        painter: QPainter,
        left: float,
        width: float,
        track: QRectF,
        radius: float,
        head: float,
    ) -> None:
        """Highlight the stretches of the file that have sound.

        Runs are merged with hysteresis (on at `_ON_LEVEL`, off at `_OFF_LEVEL`,
        closing only after `_GAP_BUCKETS` quiet buckets) so a single quiet syllable
        does not chop a sentence, and unanalysed buckets close a run at the last
        bucket that really had sound instead of trailing across the unknown part.
        """
        levels = self._levels or []
        count = len(levels)
        if count == 0 or width <= 0.0:
            return

        runs: list[tuple[int, int]] = []
        active = False
        start = 0
        quiet = 0
        last_sound = 0
        for index, level in enumerate(levels):
            if level < 0:
                if active:
                    runs.append((start, last_sound + 1))
                    active = False
                quiet = 0
                continue
            if not active:
                if level >= _ON_LEVEL:
                    active, start, quiet, last_sound = True, index, 0, index
                continue
            if level >= _OFF_LEVEL:
                quiet = 0
                last_sound = index
                continue
            quiet += 1
            if quiet >= _GAP_BUCKETS:
                runs.append((start, last_sound + 1))
                active = False
        if active:
            runs.append((start, count))

        # Square corners on the highlights: they are data, not chips. The rail's
        # own rounded ends are the only curve, so the runs are clipped to it.
        rail = QPainterPath()
        rail.addRoundedRect(track, radius, radius)
        for played_side, alpha in ((False, _SOUND_ALPHA), (True, _SOUND_ON_PLAYED)):
            tint = QColor(tokens.ACCENT if played_side else tokens.ACCENT_GREEN)
            tint.setAlphaF(alpha)
            painter.save()
            painter.setClipPath(rail)
            painter.setClipRect(
                QRectF(left, track.top(), max(0.0, head - left), track.height())
                if played_side
                else QRectF(head, track.top(), max(0.0, left + width - head), track.height()),
                Qt.ClipOperation.IntersectClip,
            )
            painter.setBrush(tint)
            span = self._duration if self._duration > 0 else count * self._bucket_ms
            right = left + width
            for start, end in runs:
                x0 = left + width * min(1.0, start * self._bucket_ms / span)
                x1 = left + width * min(1.0, end * self._bucket_ms / span)
                x0, x1 = max(left, min(right, x0)), max(left, min(right, x1))
                if x1 - x0 < 1.0:
                    continue
                painter.drawRect(QRectF(x0, track.top(), x1 - x0, track.height()))
            painter.restore()

    def set_levels(self, levels: list[int] | None, bucket_ms: int = 250) -> None:
        """Loudness map for the whole file (None = plain track until it exists).

        `bucket_ms` matters: an audio track is often shorter than the video (a
        film's credits are silent, some muxes end the audio early), so buckets
        must be placed by *time*, never by index over the bucket count - doing
        that stretched the map across the whole rail and drifted by seconds.
        """
        self._levels = levels or None
        self._bucket_ms = max(1, int(bucket_ms))
        self.update()

    def _paint_bubble(self, painter: QPainter, y: float, thickness: float) -> None:
        text = format_time(self._position_for(self._hover_x), needs_hours(self._duration))
        font = QFont(tokens.FONT_FAMILY)
        font.setPixelSize(tokens.FS_TITLE)
        font.setWeight(QFont.Weight.Medium)
        painter.setFont(font)
        width = painter.fontMetrics().horizontalAdvance(text) + 14.0
        height = min(float(tokens.BUBBLE_H) - 2.0, y - thickness / 2.0 - 3.0)
        x = self._hover_x - width / 2.0
        x = min(max(x, 0.0), self.width() - width)
        top = y - thickness / 2.0 - 3.0 - height
        bubble = QRectF(x, top, width, height)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(tokens.MENU_BG))
        painter.drawRoundedRect(bubble, 6.0, 6.0)
        painter.setPen(QPen(QColor(tokens.TEXT)))
        painter.drawText(bubble, Qt.AlignmentFlag.AlignCenter, text)
