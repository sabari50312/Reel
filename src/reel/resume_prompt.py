"""The "continue where you left off?" panel.

Same constraint as the overlay: it cannot be a child widget of the main window,
because libVLC's video is a native child that would paint over it. So it is a
small frameless top-level window, owned by the player, styled from the tokens.

Keyboard: Enter continues, Esc starts over - there is no third state, and the
panel disappears the moment a choice is made.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QKeyEvent, QShowEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .style import tokens
from .widgets.timefmt import format_time, needs_hours


class ResumePrompt(QWidget):
    chosen = Signal(bool)  # True = continue, False = start over

    def __init__(self, host: QWidget) -> None:
        super().__init__(host, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self._host = host
        self.setObjectName("ResumePrompt")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle("Continue playing")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        panel = QFrame(self)
        panel.setObjectName("ResumePanel")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(panel)

        title = QLabel("Pick up where you left off?", panel)
        title.setObjectName("ResumeTitle")
        self._when = QLabel("", panel)
        self._when.setObjectName("ResumeWhen")

        self._continue = QPushButton("Continue", panel)
        self._continue.setObjectName("PromptPrimary")
        self._start = QPushButton("Start over", panel)
        self._start.setObjectName("PromptSecondary")
        self._continue.setDefault(True)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch(1)
        buttons.addWidget(self._start)
        buttons.addWidget(self._continue)

        inner = QVBoxLayout(panel)
        inner.setContentsMargins(22, 18, 22, 18)
        inner.setSpacing(6)
        inner.addWidget(title)
        inner.addWidget(self._when)
        inner.addSpacing(10)
        inner.addLayout(buttons)

        self._continue.clicked.connect(lambda: self._finish(True))
        self._start.clicked.connect(lambda: self._finish(False))
        self.hide()

    # ------------------------------------------------------------------ public
    def ask(self, milliseconds: int) -> None:
        hours = needs_hours(milliseconds)
        self._when.setText(f"Continue from {format_time(milliseconds, hours)}?")
        self.adjustSize()
        self._centre_on_host()
        self.show()
        self.raise_()
        self.activateWindow()
        self._continue.setFocus()

    def answer(self, cont: bool) -> None:
        self._finish(cont)

    # ----------------------------------------------------------------- internal
    def _centre_on_host(self) -> None:
        centre = self._host.mapToGlobal(self._host.rect().center())
        self.move(QPoint(centre.x() - self.width() // 2, centre.y() - self.height() // 2))

    def _finish(self, cont: bool) -> None:
        if not self.isVisible():
            return
        self.hide()
        self.chosen.emit(cont)

    # ------------------------------------------------------------------- events
    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._centre_on_host()  # adjustSize() before show changes the size

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._finish(True)
            return
        if event.key() == Qt.Key.Key_Escape:
            self._finish(False)
            return
        super().keyPressEvent(event)


__all__ = ["ResumePrompt"]
