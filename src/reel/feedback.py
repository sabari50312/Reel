"""Transient on-screen feedback: the centre mute icon and the volume pill.

Same HWND problem as the overlay, same answer: a separate frameless, translucent,
owned top-level window. Two differences matter:

  WindowTransparentForInput   the whole client rect is covered while a flash is
                              visible, so the window must be a hole for the
                              mouse (WS_EX_TRANSPARENT) or it would eat clicks
  painted alpha               a per-pixel-alpha layered window ignores
                              setWindowOpacity (Qt then uses UpdateLayeredWindow),
                              so the fade is painted, exactly like the scrim

The window is hidden outright between flashes, so nothing lingers above the
video. Its geometry follows the host, not the strip: the disc sits in the centre
of the picture and the pill on the right edge.
"""

from __future__ import annotations

from PySide6.QtCore import (
    Property,
    QEvent,
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    QTimer,
    Qt,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPaintEvent
from PySide6.QtWidgets import QMainWindow, QWidget

from .style import tokens
from .widgets.icons import paint_icon

_PILL_MARGIN = 24  # gap between the pill and the right edge of the video
_PILL_ICON = 26
_BAR_W = 10.0
_BAR_H = 200.0


class Feedback(QWidget):
    """Brief centre disc or right-hand level pill over the video."""

    def __init__(self, host: QMainWindow) -> None:
        super().__init__(
            host,
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.NoDropShadowWindowHint,
        )
        self._host = host
        self.setObjectName("Feedback")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setWindowTitle("Reel feedback")

        self._alpha = 0.0
        self._target = 0.0
        self._mode = ""  # "icon" | "level"
        self._icon = ""
        self._fraction = 0.0
        self._text = ""
        self._suppressed = False

        self._fade = QPropertyAnimation(self, b"alpha", self)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.finished.connect(self._on_fade_finished)

        self._hold = QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.timeout.connect(self._fade_out)

        host.installEventFilter(self)

    # ------------------------------------------------------------------- sugar
    def set_suppressed(self, suppressed: bool) -> None:
        """Hide the flash and ignore new ones while another window is in front.

        The flash is a topmost layered window; without this its disc and volume
        pill hang over the settings dialog.
        """
        self._suppressed = suppressed
        if not suppressed:
            return
        self._hold.stop()
        self._fade.stop()
        self._target = 0.0
        self._alpha = 0.0
        self.hide()

    def show_icon(self, icon: str) -> None:
        """Centre disc, e.g. the mute state after M."""
        self._mode = "icon"
        self._icon = icon
        self._present()

    def show_text(self, text: str) -> None:
        """Bottom-centre caption chip, e.g. the playback speed after ], [ or =."""
        self._mode = "text"
        self._text = text
        self._present()

    def show_level(self, icon: str, fraction: float, text: str) -> None:
        """Right-hand vertical pill with a fill and a caption."""
        self._mode = "level"
        self._icon = icon
        self._fraction = max(0.0, min(1.0, fraction))
        self._text = text
        self._present()
    # -------------------------------------------------------------- animation
    def get_alpha(self) -> float:
        return self._alpha

    def set_alpha(self, value: float) -> None:
        if value != self._alpha:
            self._alpha = value
            self.update()

    alpha = Property(float, get_alpha, set_alpha)

    def _present(self) -> None:
        """Show or refresh the flash.

        Refreshes must be cheap: a held volume key repeats many times a second,
        and re-setting the window geometry plus restarting the fade animation on
        every repeat is what made the pill lag behind the key presses. So the
        geometry is only touched when it actually changed, the fade-in only runs
        when the window is not already fully visible, and the repaint is explicit
        because new content does not move the alpha property.
        """
        if self._suppressed:
            return
        self._hold.stop()
        target = QRect(self._host.mapToGlobal(QPoint(0, 0)), self._host.size())
        if self.geometry() != target:
            self.setGeometry(target)
        if not self.isVisible():
            self.show()  # WA_ShowWithoutActivating + TransparentForInput
            self.raise_()
        self.update()
        self._target = 1.0
        if self._alpha < 0.99:
            self._fade.stop()
            self._fade.setDuration(tokens.FLASH_IN_MS)
            self._fade.setStartValue(self._alpha)
            self._fade.setEndValue(1.0)
            self._fade.start()
        self._hold.start(tokens.FLASH_IN_MS + tokens.FLASH_HOLD_MS)

    def _fade_out(self) -> None:
        self._fade.stop()
        self._target = 0.0
        self._fade.setDuration(tokens.FLASH_OUT_MS)
        self._fade.setStartValue(self._alpha)
        self._fade.setEndValue(0.0)
        self._fade.start()

    def _on_fade_finished(self) -> None:
        if self._target <= 0.0 and self._alpha <= 0.01:
            self.hide()

    # --------------------------------------------------------------- geometry
    def sync_geometry(self) -> None:
        self.setGeometry(QRect(self._host.mapToGlobal(QPoint(0, 0)), self._host.size()))

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if watched is self._host:
            if event.type() in (QEvent.Type.Move, QEvent.Type.Resize):
                if self.isVisible():
                    self.sync_geometry()
            elif event.type() in (QEvent.Type.Hide, QEvent.Type.Close):
                self.hide()
        return super().eventFilter(watched, event)

    # ---------------------------------------------------------------- painting
    def paintEvent(self, event: QPaintEvent) -> None:
        if self._alpha <= 0.0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setOpacity(self._alpha)
        if self._mode == "icon":
            self._paint_disc(painter)
        elif self._mode == "level":
            self._paint_pill(painter)
        elif self._mode == "text":
            self._paint_chip(painter)
        painter.end()

    def _paint_disc(self, painter: QPainter) -> None:
        centre = QRectF(self.rect()).center()
        radius = tokens.FLASH_DISC / 2.0
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 140))
        painter.drawEllipse(centre, radius, radius)
        side = radius * 0.72
        icon_rect = QRectF(centre.x() - side, centre.y() - side, side * 2, side * 2)
        paint_icon(painter, self._icon, icon_rect, tokens.TEXT, stroke=2.2)

    def _paint_chip(self, painter: QPainter) -> None:
        """Rounded label near the bottom edge, above where the strip sits."""
        font = painter.font()
        font.setPixelSize(15)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(self._text) + 32
        height = 38
        x = (self.width() - width) / 2.0
        y = self.height() - tokens.BAR_H - 52 - height
        painter.setPen(QPen(QColor(tokens.BAR_BORDER), 1.0))
        painter.setBrush(QColor(tokens.MENU_BG))
        painter.drawRoundedRect(QRectF(x, y, width, height), 12.0, 12.0)
        painter.setPen(QColor(tokens.TEXT))
        painter.drawText(QRectF(x, y, width, height), Qt.AlignmentFlag.AlignCenter, self._text)

    def _paint_pill(self, painter: QPainter) -> None:
        x = self.width() - _PILL_MARGIN - tokens.PILL_W
        y = (self.height() - tokens.PILL_H) / 2.0
        painter.setPen(QPen(QColor(tokens.BAR_BORDER), 1.0))
        painter.setBrush(QColor(tokens.MENU_BG))
        painter.drawRoundedRect(
            QRectF(x, y, tokens.PILL_W, tokens.PILL_H), tokens.PILL_W / 2.0, tokens.PILL_W / 2.0
        )

        icon_rect = QRectF(
            x + (tokens.PILL_W - _PILL_ICON) / 2.0, y + 18, _PILL_ICON, _PILL_ICON
        )
        paint_icon(painter, self._icon, icon_rect, tokens.TEXT)

        bar_x = x + (tokens.PILL_W - _BAR_W) / 2.0
        bar_y = y + 58.0
        painter.setBrush(QColor(255, 255, 255, 72))
        painter.drawRoundedRect(QRectF(bar_x, bar_y, _BAR_W, _BAR_H), _BAR_W / 2, _BAR_W / 2)
        if self._fraction > 0.0:
            fill_h = _BAR_H * self._fraction
            painter.setBrush(QColor(tokens.TEXT))
            painter.drawRoundedRect(
                QRectF(bar_x, bar_y + _BAR_H - fill_h, _BAR_W, fill_h), _BAR_W / 2, _BAR_W / 2
            )

        font = painter.font()
        font.setFamily(tokens.FONT_FAMILY)
        font.setPixelSize(tokens.FS_TIME)
        painter.setFont(font)
        painter.setPen(QColor(tokens.TEXT))
        painter.drawText(
            QRectF(x, bar_y + _BAR_H + 12.0, tokens.PILL_W, 20.0),
            Qt.AlignmentFlag.AlignCenter,
            self._text,
        )


__all__ = ["Feedback"]
