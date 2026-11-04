"""Volume: mute toggle plus a horizontal slider.

The slider is always visible - no hover reveal. Unfolding it on hover meant the
rail vanished the moment the pointer left, which read as the control breaking,
and the fixed width already keeps the layout stable.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPaintEvent, QPainter, QWheelEvent
from PySide6.QtWidgets import QWidget

from ..style import tokens
from .iconbutton import IconButton

_PAD = 4  # gap between mute button and slider
_RAIL_H = float(tokens.SEEK_H)  # same rail as the seek bar: one family
_KNOB = 6.0  # minimum fill span, so 1% still reads as something
_SLIDER_H = 20  # hit height of the slider strip
_STEP = 5  # wheel / arrow step, in percent


class VolumeBar(QWidget):
    volume_changed = Signal(int)  # 0..100
    muted_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Small button size: the volume control sits in the one-row card next to
        # 34px utility buttons and must not stand out as a 40px block.
        self.setFixedSize(tokens.BTN_SM + _PAD + tokens.VOL_W, tokens.BTN_SM)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._volume = 100
        self._muted = False
        self._hover = False
        self._dragging = False

        self.button = IconButton(
            "volume_high", self, size=tokens.BTN_SM, icon_size=20, tip="Mute (M)"
        )
        self.button.move(0, (self.height() - tokens.BTN_SM) // 2)
        self.button.clicked.connect(self.toggle_mute)

    # ----------------------------------------------------------------- state
    def volume_value(self) -> int:
        return self._volume

    def set_volume(self, volume: int, muted: bool | None = None) -> None:
        volume = max(0, min(100, int(volume)))
        changed = volume != self._volume
        self._volume = volume
        if muted is not None:
            self._muted = muted
        if changed or muted is not None:
            self._refresh_icon()
            self.update()

    def set_muted(self, muted: bool) -> None:
        if muted != self._muted:
            self._muted = muted
            self._refresh_icon()
            self.update()

    def toggle_mute(self) -> None:
        self._muted = not self._muted
        self._refresh_icon()
        self.update()
        self.muted_changed.emit(self._muted)

    def _refresh_icon(self) -> None:
        if self._muted or self._volume == 0:
            name = "volume_mute"
        elif self._volume < 50:
            name = "volume_low"
        else:
            name = "volume_high"
        self.button.set_icon_name(name)
        self.button.setToolTip("Unmute (M)" if self._muted else "Mute (M)")

    # --------------------------------------------------------------- geometry
    def _slider_rect(self) -> QRectF:
        left = float(tokens.BTN_SM + _PAD)
        return QRectF(left, (self.height() - _SLIDER_H) / 2.0, float(tokens.VOL_W), _SLIDER_H)

    def _fraction_at(self, x: float) -> float:
        rect = self._slider_rect()
        return min(1.0, max(0.0, (x - rect.left()) / max(1.0, rect.width())))

    def _volume_at(self, x: float) -> int:
        """0..100 for a widget-space x. (The fraction is 0..1; the engine is 0..100.)"""
        return int(round(self._fraction_at(x) * 100.0))

    # ----------------------------------------------------------------- input
    def _over_slider(self, x: float) -> bool:
        return self._slider_rect().adjusted(-_PAD, 0, 0, 0).contains(x, self.height() / 2.0)

    def enterEvent(self, event) -> None:  # noqa: ANN001 - Qt signature
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: ANN001 - Qt signature
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._over_slider(event.position().x()):
            self._dragging = True
            self._emit_volume(self._volume_at(event.position().x()))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._dragging:
            self._emit_volume(self._volume_at(event.position().x()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if delta:
            self._emit_volume(self._volume + (_STEP if delta > 0 else -_STEP))
            event.accept()
            return
        super().wheelEvent(event)

    def _emit_volume(self, volume: int) -> None:
        volume = max(0, min(100, int(volume)))
        if volume == self._volume and not self._muted:
            return
        self._volume = volume
        if volume > 0:  # any explicit volume move unmutes
            was_muted = self._muted
            self._muted = False
        else:
            was_muted = False
        self._refresh_icon()
        self.update()
        if was_muted:
            self.muted_changed.emit(False)
        self.volume_changed.emit(volume)

    # ----------------------------------------------------------------- paint
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.EnabledChange:
            self.button.setEnabled(self.isEnabled())
        super().changeEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = self._slider_rect()
        width = rect.width()
        # Same weight as the seek bar's blocks, and rounded by exactly half its
        # height: a fatter rail with a rounder cap, never a pinched tip.
        height = _RAIL_H
        top = rect.center().y() - height / 2.0
        track = QRectF(rect.left(), top, width, height)
        radius = height / 2.0

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(tokens.BLOCK_OFF if not self._hover else tokens.BLOCK_ON))
        painter.drawRoundedRect(track, radius, radius)

        fraction = 0.0 if self._muted else self._volume / 100.0
        if fraction > 0.0:
            span = max(_KNOB, width * fraction)  # a 1% volume still reads as a cap
            painter.save()
            painter.setClipRect(QRectF(track.left(), top, min(span, width), height))
            veil = QColor(tokens.ACCENT)
            veil.setAlphaF(0.45)
            painter.setBrush(veil)
            painter.drawRoundedRect(track, radius, radius)
            painter.restore()

        painter.end()
