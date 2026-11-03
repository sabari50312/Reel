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
from PySide6.QtGui import QColor, QEnterEvent, QFont, QMouseEvent, QPaintEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ..style import tokens
from .timefmt import format_time, needs_hours

SEEKBAR_H = tokens.SEEK_HIT + tokens.BUBBLE_H
_TRACK_PAD = tokens.SEEK_KNOB / 2.0  # keeps the knob inside the widget


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
        return self.height() - tokens.SEEK_HIT / 2.0

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

        thickness = tokens.SEEK_H + (tokens.SEEK_H_HOVER - tokens.SEEK_H) * self._grow
        y = self._track_y()
        left = _TRACK_PAD
        track = QRectF(left, y - thickness / 2.0, self._usable_width(), thickness)
        radius = thickness / 2.0

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(tokens.TRACK_HOVER if self._hover else tokens.TRACK))
        painter.drawRoundedRect(track, radius, radius)

        fraction = (self._position / self._duration) if self._duration > 0 else 0.0
        fraction = min(1.0, max(0.0, fraction))
        if fraction > 0.0:
            played = QRectF(left, y - thickness / 2.0, self._usable_width() * fraction, thickness)
            painter.setBrush(QColor(tokens.ACCENT))
            painter.drawRoundedRect(played, radius, radius)

        if self._hover or self._dragging:
            knob_x = self._x_for(fraction)
            r = tokens.SEEK_KNOB / 2.0
            painter.setBrush(QColor(tokens.ACCENT))
            painter.drawEllipse(QRectF(knob_x - r, y - r, 2 * r, 2 * r))

        if self._hover and self._duration > 0:
            self._paint_bubble(painter, y, thickness)
        painter.end()

    def _paint_bubble(self, painter: QPainter, y: float, thickness: float) -> None:
        text = format_time(self._position_for(self._hover_x), needs_hours(self._duration))
        font = QFont(tokens.FONT_FAMILY)
        font.setPixelSize(tokens.FS_TITLE)
        painter.setFont(font)
        width = painter.fontMetrics().horizontalAdvance(text) + 14.0
        height = float(tokens.BUBBLE_H) - 2.0
        x = self._hover_x - width / 2.0
        x = min(max(x, 0.0), self.width() - width)
        top = y - thickness / 2.0 - 6.0 - height
        bubble = QRectF(x, top, width, height)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(tokens.TOAST_BG))
        painter.drawRoundedRect(bubble, tokens.RADIUS_CTL, tokens.RADIUS_CTL)
        painter.setPen(QPen(QColor(tokens.TEXT)))
        painter.drawText(bubble, Qt.AlignmentFlag.AlignCenter, text)
