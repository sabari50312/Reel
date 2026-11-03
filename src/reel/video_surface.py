"""The video surface: a native child window that libVLC paints into.

Why a separate class at all: QWidget must be *explicitly* native before we can
hand its HWND to libVLC. Without WA_NativeWindow Qt may re-parent the widget
later and detach VLC's video output, which shows up as a black player.

Two hard rules enforced here:

1. **No child widgets.** libVLC and Qt share one HWND, so any Qt child inside
   this widget (a QLabel "open a file" hint, for example) paints over VLC's
   frames — Qt's backing store always wins. The empty state is therefore
   painted by paintEvent, and dropped entirely once the video output exists.
2. **Updates are switched off while playing.** Resizes, stylesheet changes and
   focus events all trigger repaints; every repaint is a frame Qt paints over
   VLC. setUpdatesEnabled(False) makes that impossible.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPaintEvent, QPainter
from PySide6.QtWidgets import QWidget

from .style import tokens


class VideoSurface(QWidget):
    activity = Signal()  # any mouse motion: wake the controls
    clicked = Signal()
    double_clicked = Signal()
    wheel = Signal(int)  # wheel delta: volume, without touching the strip

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # Own our own HWND, and never let Qt clear it: VLC is drawing there.
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)

        # Hover must reach us with no button pressed, or the controls never
        # wake: Qt only sends mouseMoveEvent while a button is held otherwise.
        self.setMouseTracking(True)

        self._hwnd = 0
        self._empty_text: str | None = None

    # ------------------------------------------------------------------ input
    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self.activity.emit()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: ANN001 - QWheelEvent
        self.wheel.emit(event.angleDelta().y())
        event.accept()

    def hwnd(self) -> int:
        """Stable HWND for libVLC. Forces native window creation on first call."""
        if not self._hwnd:
            self._hwnd = int(self.winId())
        return self._hwnd

    # ------------------------------------------------------------- empty state
    def set_empty_text(self, text: str | None) -> None:
        """Centred dim message for the no-media state. None = let VLC own the pixels."""
        self._empty_text = text
        self.setUpdatesEnabled(text is not None)
        if text is not None:
            self.update()

    def empty_text(self) -> str | None:
        return self._empty_text

    # ---------------------------------------------------------------- painting
    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(tokens.VIDEO_BG))
        if not self._empty_text:
            return

        font = painter.font()
        font.setFamily(tokens.FONT_FAMILY)
        font.setPixelSize(tokens.FS_TIME)
        painter.setFont(font)
        painter.setPen(QColor(tokens.TEXT_FAINT))
        painter.drawText(
            QRect(self.rect()).adjusted(tokens.PAD, 0, -tokens.PAD, 0),
            Qt.AlignmentFlag.AlignCenter,
            self._empty_text,
        )
        painter.end()
