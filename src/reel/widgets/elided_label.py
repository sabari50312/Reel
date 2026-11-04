"""A label that never widens its row.

Sidebars are narrow, and a label's natural width is whatever its text needs.
Letting that drive the layout pushed the settings panel's content wider than its
viewport (the keybindings holder measured 480 px inside a 430 px viewport). This
label reports a zero width hint and paints an ellipsis instead, so the row is
sized by the controls and the full text stays reachable as a tooltip.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget


class ElidedLabel(QLabel):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full = text
        self.setToolTip(text)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def resizeEvent(self, event) -> None:  # noqa: ANN001 - Qt signature
        super().resizeEvent(event)
        self._apply()

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        self._full = text
        self._apply()

    def full_text(self) -> str:
        return self._full

    def _apply(self) -> None:
        metrics = self.fontMetrics()
        elided = metrics.elidedText(self._full, Qt.TextElideMode.ElideRight, max(24, self.width()))
        super().setText(elided)


__all__ = ["ElidedLabel"]
