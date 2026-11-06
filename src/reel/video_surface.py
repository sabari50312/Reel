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

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QMouseEvent,
    QPaintEvent,
    QPainter,
)
from PySide6.QtWidgets import QWidget

from .style import tokens


class VideoSurface(QWidget):
    activity = Signal()  # any mouse motion: wake the controls
    clicked = Signal()
    double_clicked = Signal()
    wheel = Signal(int)  # wheel delta: volume, without touching the strip
    menu_requested = Signal(QPoint)  # global position of a right click

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
        elif event.button() == Qt.MouseButton.RightButton:
            # Right click opens the menu and *never* toggles playback, so it does
            # not go through `clicked`.
            self.menu_requested.emit(event.globalPosition().toPoint())
            event.accept()
            return
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
        if not self._empty_text:
            painter.fillRect(self.rect(), QColor(tokens.VIDEO_BG))
            painter.end()
            return

        # Home screen: one flat surface, Material style. A gradient and a glow
        # both read as decoration next to the flat cards and menus, so the empty
        # state is a single colour with the wordmark and hint doing the work.
        rect = self.rect()
        painter.fillRect(rect, QColor(tokens.HOME_TOP))

        # The wordmark is the one place Reel speaks in a face of its own (Sora,
        # bundled and registered at startup); the hint under it uses the same
        # grotesk as the rest of the UI so the screen reads as one design.
        mark = QFont(tokens.FONT_DISPLAY)
        mark.setPixelSize(max(26, min(46, rect.height() // 13)))
        mark.setWeight(QFont.Weight.DemiBold)
        mark.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
        painter.setFont(mark)
        painter.setPen(QColor(tokens.ACCENT_GREEN))
        mark_rect = QRect(rect)
        mark_rect.setBottom(rect.center().y() + 6)
        painter.drawText(mark_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom, "Reel")

        font = painter.font()
        font.setFamily(tokens.FONT_FAMILY)
        font.setPixelSize(tokens.FS_TIME)
        font.setWeight(QFont.Weight.Medium)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.0)
        painter.setFont(font)
        painter.setPen(QColor(tokens.TEXT_DIM))
        hint_rect = QRect(rect)
        hint_rect.setTop(rect.center().y() + 16)
        painter.drawText(hint_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, self._empty_text)
        painter.end()
