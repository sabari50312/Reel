"""The overlay: controls living in their own always-on-top window.

This is the consequence of HWND embedding, not a styling choice. libVLC paints
into a native child window, and Windows composites native child windows *above*
everything Qt draws in the same window. So a QPushButton next to the video would
be invisible. VLC's own Qt frontend solves it the same way: a separate frameless,
translucent, owned top-level window pinned to the player's client rect.

Window flags that matter:
  Qt.Window                  owned by the main window: stays above it, closes with it,
                             no taskbar button. NOT Qt.Tool, because Qt hides tool
                             windows whenever the application deactivates - the
                             controls would vanish the moment focus moves elsewhere
  FramelessWindowHint        + WA_TranslucentBackground => no frame, real alpha
  WindowDoesNotAcceptFocus   clicking play/pause must never deactivate the player
  WA_ShowWithoutActivating   showing the overlay must not steal focus either

Only the bottom strip is covered, so the rest of the video stays clickable and
the overlay can be hidden outright (no invisible click-eater when it is idle).
"""

from __future__ import annotations

from PySide6.QtCore import (
    Property,
    QEvent,
    QEasingCurve,
    QObject,
    QPropertyAnimation,
    QRect,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QLinearGradient, QMouseEvent, QPaintEvent, QPainter
from PySide6.QtWidgets import QMainWindow, QWidget

from .style import tokens

_BOTTOM_MARGIN = 10  # content padding inside the strip's bottom edge
_WAKE_EVENTS = (
    QEvent.Type.Enter,
    QEvent.Type.MouseMove,
    QEvent.Type.HoverMove,
    QEvent.Type.Wheel,
)


class _Scrim(QWidget):
    """Bottom gradient. Pure painting, never takes the mouse.

    The fade is animated as *painted* alpha, not window opacity. Qt maps a
    translucent top-level window on Windows to a per-pixel-alpha layered window
    (WS_EX_LAYERED + UpdateLayeredWindow), where setWindowOpacity does not
    reliably reach the screen: the buffer pushed while opacity was 0.0 stays
    invisible until something repaints. Painting the alpha guarantees a fresh
    buffer every frame.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._alpha = 0.0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)

    def get_alpha(self) -> float:
        return self._alpha

    def set_alpha(self, value: float) -> None:
        if value != self._alpha:
            self._alpha = value
            self.update()

    fadeAlpha = Property(float, get_alpha, set_alpha)

    def paintEvent(self, event: QPaintEvent) -> None:
        if self._alpha <= 0.0:
            return
        painter = QPainter(self)
        gradient = QLinearGradient(0, 0, 0, self.height())
        for stop, color in (
            (0.0, tokens.SCRIM_START),
            (0.55, tokens.SCRIM_MID),
            (1.0, tokens.SCRIM),
        ):
            scaled = QColor(color)
            scaled.setAlphaF(scaled.alphaF() * self._alpha)
            gradient.setColorAt(stop, scaled)
        painter.fillRect(self.rect(), gradient)
        painter.end()


class Overlay(QWidget):
    """Auto-hiding strip of controls pinned to the bottom of the video area."""

    play_toggle_requested = Signal()
    visibility_changed = Signal(bool)  # True = strip shown, False = faded out

    def __init__(self, host: QMainWindow) -> None:
        super().__init__(
            host,
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.NoDropShadowWindowHint,
        )
        self._host = host
        self.setObjectName("Overlay")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setWindowTitle("Reel controls")

        self._scrim = _Scrim(self)

        self._fade = QPropertyAnimation(self._scrim, b"fadeAlpha", self)
        self._fade.setDuration(tokens.FADE_MS)
        self._fade.setEasingCurve(QEasingCurve.Type.Linear)
        self._fade.finished.connect(self._on_fade_finished)

        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.setInterval(tokens.HIDE_MS)
        self._idle.timeout.connect(self.sleep)

        self._visible = False
        self._hold = False
        self._content: QWidget | None = None

        host.installEventFilter(self)

    # ---------------------------------------------------------------- content
    def set_content(self, widget: QWidget) -> None:
        """Place the control strip inside the overlay, above the scrim.

        The widget paints itself; the overlay only positions it and watches it
        for mouse activity so hovering the controls keeps them from vanishing.
        """
        self._content = widget
        widget.setParent(self)
        widget.raise_()  # content above the scrim
        self._watch(widget)
        self.sync_geometry()

    def content(self) -> QWidget | None:
        return self._content

    def _watch(self, widget: QWidget) -> None:
        """Track hover on the strip and every control inside it."""
        widget.installEventFilter(self)
        widget.setMouseTracking(True)
        for child in widget.findChildren(QWidget):
            child.installEventFilter(self)
            child.setMouseTracking(True)

    def hold(self, active: bool) -> None:
        """Freeze auto-hide while the user is dragging something."""
        self._hold = active
        if active:
            self._idle.stop()
        elif self._visible:
            self._idle.start()

    # ------------------------------------------------------------- visibility
    def wake(self) -> None:
        """Show the controls and restart the idle countdown.

        A held strip (paused playback) still shows: `hold` freezes auto-hide,
        it must not make the pause state invisible.
        """
        if not self._hold:
            self._idle.start()
        if self._visible:
            return
        self.sync_geometry()
        self._visible = True
        self.show()  # WA_ShowWithoutActivating: no focus theft
        self.raise_()
        self._animate(1.0)
        self.visibility_changed.emit(True)

    def sleep(self) -> None:
        self._idle.stop()
        if not self._visible or self._hold:
            return
        self._visible = False
        self._animate(0.0)
        self.visibility_changed.emit(False)

    def is_awake(self) -> bool:
        return self._visible

    def _animate(self, target: float) -> None:
        self._fade.stop()
        self._fade.setStartValue(self._scrim.fadeAlpha)
        self._fade.setEndValue(target)
        self._fade.start()

    def _on_fade_finished(self) -> None:
        # Hide only once fully transparent: an invisible overlay window would
        # still swallow every click on the video behind it.
        if not self._visible and self._scrim.fadeAlpha <= 0.01:
            self.hide()

    # -------------------------------------------------------------- geometry
    def sync_geometry(self) -> None:
        """Pin the strip to the bottom of the host's client area."""
        client = self._host.rect()
        height = min(tokens.OVERLAY_H, max(48, client.height()))
        bottom_left = self._host.mapToGlobal(client.bottomLeft())
        self.setGeometry(QRect(bottom_left.x(), bottom_left.y() - height + 1, client.width(), height))
        self._scrim.setGeometry(0, 0, client.width(), height)
        if self._content is not None:
            strip_h = min(self._content.height(), height)
            self._content.setGeometry(
                tokens.PAD,
                height - _BOTTOM_MARGIN - strip_h,
                max(1, client.width() - 2 * tokens.PAD),
                strip_h,
            )

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        kind = event.type()
        if watched is self._host:
            if kind in (QEvent.Type.Move, QEvent.Type.Resize):
                if self._visible:
                    self.sync_geometry()
            elif kind in (QEvent.Type.Hide, QEvent.Type.Close):
                self._idle.stop()
                self._visible = False
                self.hide()
            elif kind == QEvent.Type.WindowStateChange and self._host.isMinimized():
                self._idle.stop()
                self._visible = False
                self.hide()
        elif kind in _WAKE_EVENTS:
            self.wake()
        return False

    # ----------------------------------------------------------------- input
    def mousePressEvent(self, event: QMouseEvent) -> None:
        # Clicking empty overlay space behaves like clicking the video.
        if event.button() == Qt.MouseButton.LeftButton:
            self.play_toggle_requested.emit()
        super().mousePressEvent(event)
