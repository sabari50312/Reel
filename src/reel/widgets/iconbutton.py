"""Square, flat, icon-only button.

Plain QAbstractButton with a hand-drawn face: no border, a rounded-rect fill on
hover/press, and a 24px icon centred in a 40px hit box. The icons themselves are
painted geometry (see icons.py), so nothing needs re-tinting per theme.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QEnterEvent, QPaintEvent, QPainter
from PySide6.QtWidgets import QAbstractButton, QWidget

from ..style import tokens
from .icons import paint_icon


class IconButton(QAbstractButton):
    def __init__(
        self,
        icon_name: str,
        parent: QWidget | None = None,
        *,
        size: int = tokens.BTN,
        icon_size: int = tokens.ICON,
        tip: str = "",
    ) -> None:
        super().__init__(parent)
        self._icon_name = icon_name
        self._icon_size = icon_size
        self._hover = False
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # keyboard goes to the player
        if tip:
            self.setToolTip(tip)
        self.pressed.connect(self.update)
        self.released.connect(self.update)

    # ------------------------------------------------------------------ icon
    def icon_name(self) -> str:
        return self._icon_name

    def set_icon_name(self, name: str) -> None:
        if name != self._icon_name:
            self._icon_name = name
            self.update()

    def sizeHint(self) -> QSize:
        return QSize(self.width(), self.height())

    # ------------------------------------------------------------------ paint
    def enterEvent(self, event: QEnterEvent) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: ANN001 - Qt signature
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        if self._hover or self.isDown():
            if self.isDown():
                fill = tokens.BTN_ACTIVE
            else:
                fill = tokens.BTN_HOVER
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(fill))
            painter.drawRoundedRect(QRectF(self.rect()), tokens.RADIUS_CTL, tokens.RADIUS_CTL)

        color = tokens.TEXT if self.isEnabled() else tokens.TEXT_FAINT
        side = float(min(self._icon_size, self.width(), self.height()))
        rect = QRectF(
            (self.width() - side) / 2.0,
            (self.height() - side) / 2.0,
            side,
            side,
        )
        paint_icon(painter, self._icon_name, rect, color)
        painter.end()
