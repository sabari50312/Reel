"""Shortcut chips and one row per action.

Split out of the settings UI so the sidebar only has to place rows; everything
that knows how a binding is captured, replaced or cleared lives here.

Rules that matter:
  * **Capture, do not type.** Clicking a chip and pressing the keys you want is
    the gesture; nothing parses "Ctrl+Shift+Left" by hand.
  * **Steal with a receipt.** A key already in use moves to the new action and
    the panel reports which action lost it - refusing would be a dead end, and
    stealing silently would be a mystery later.
  * **Rows do not refresh themselves.** One captured key can belong to another
    row too, so the panel rebuilds every row when a binding changes.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFocusEvent, QKeyEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import shortcuts
from .widgets.elided_label import ElidedLabel

MAX_BINDINGS = 3  # more than this is a misclick, not intent


class BindingChip(QPushButton):
    """A shortcut pill: click it, then press the key you want."""

    captured = Signal(str)
    cleared = Signal()
    cancelled = Signal()

    def __init__(self, sequence: str, parent: QWidget | None = None) -> None:
        super().__init__(sequence, parent)
        self._sequence = sequence
        self._capturing = False
        self.setObjectName("BindingChip")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.clicked.connect(self.start_capture)

    # --------------------------------------------------------------- capture
    def start_capture(self) -> None:
        self._capturing = True
        self.setText("Press a key\u2026")
        self._set_property("capturing", True)
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def _end_capture(self) -> None:
        self._capturing = False
        self.setText(self._sequence)
        self._set_property("capturing", False)

    def _set_property(self, name: str, value: bool) -> None:
        # Qt only re-evaluates attribute selectors after an explicit repolish.
        self.setProperty(name, value)
        style = self.style()
        style.unpolish(self)
        style.polish(self)
        self.update()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if not self._capturing:
            super().keyPressEvent(event)
            return

        key = event.key()
        if key == Qt.Key.Key_Escape:  # cancel, do not close the window
            self._end_capture()
            self.cancelled.emit()
            return
        if key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            self._end_capture()
            self.cleared.emit()
            return

        text = shortcuts.sequence_of_key(key, event.modifiers())
        if text is None:  # a modifier pressed on its own: keep waiting
            return
        self._end_capture()
        self.captured.emit(text)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        was_capturing = self._capturing
        if was_capturing:
            self._end_capture()
        super().focusOutEvent(event)
        if was_capturing:
            self.cancelled.emit()


class ActionRow(QWidget):
    """One action: its name, its shortcuts, and a reset for that row.

    Every edit goes through `KeyBindings`, so the row does not refresh itself:
    the window listens to `Settings.changed` and rebuilds all rows (a captured key
    may have been taken from a *different* row, which also has to repaint).
    """

    def __init__(
        self,
        action: shortcuts.Action,
        bindings: shortcuts.KeyBindings,
        notify: Callable[[str], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.action = action
        self._bindings = bindings
        self._notify = notify
        self._pending = False  # a new, not-yet-bound chip is waiting for a key

        self._name = ElidedLabel(action.label, self)
        self._name.setObjectName("ActionName")

        self._slots = QHBoxLayout()
        self._slots.setContentsMargins(0, 0, 0, 0)
        self._slots.setSpacing(6)

        self._add = QPushButton("+", self)
        self._add.setObjectName("AddBinding")
        self._add.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add.setToolTip("Add another shortcut for this action")
        self._add.clicked.connect(self._on_add)

        self._reset = QPushButton("Reset", self)
        self._reset.setObjectName("RowReset")
        self._reset.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reset.clicked.connect(lambda: self._reset_row())

        # Name on its own line, chips underneath: three chips plus a name, an add
        # and a reset button do not fit side by side in a sidebar.
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        header.addWidget(self._name, 1)  # takes the spare width, then elides
        header.addWidget(self._add)
        header.addWidget(self._reset)

        self._slots.setSpacing(6)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 6)
        layout.setSpacing(4)
        layout.addLayout(header)
        layout.addLayout(self._slots)

        self.refresh()

    # ------------------------------------------------------------- rendering
    def search_title(self) -> str:
        return self.action.label

    def search_hint(self) -> str:
        return " ".join(self._bindings.keys(self.action.id))

    def refresh(self) -> None:
        while self._slots.count():
            item = self._slots.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # Detach now, delete later: deleteLater() alone leaves the old chip
                # parented (visible over the new one, and still returned by
                # findChildren) until the event loop gets to it.
                widget.setParent(None)
                widget.deleteLater()

        keys = self._bindings.keys(self.action.id)
        for index, sequence in enumerate(keys):
            chip = BindingChip(sequence, self)
            chip.captured.connect(lambda text, i=index: self._on_captured(i, text))
            chip.cleared.connect(lambda i=index: self._on_cleared(i))
            self._slots.addWidget(chip)

        pending_chip: BindingChip | None = None
        if self._pending:
            pending_chip = BindingChip("", self)
            pending_chip.captured.connect(self._on_added)
            pending_chip.cancelled.connect(self._cancel_pending)
            self._slots.addWidget(pending_chip)

        if not keys and not self._pending:
            placeholder = QLabel("unbound", self)
            placeholder.setObjectName("Unbound")
            self._slots.addWidget(placeholder)

        self._slots.addStretch(0)
        self._add.setVisible(len(keys) < MAX_BINDINGS and not self._pending)
        self._reset.setEnabled(not self._bindings.is_default(self.action.id))

        if pending_chip is not None:
            pending_chip.start_capture()

    # ---------------------------------------------------------------- edits
    def _on_captured(self, index: int, sequence: str) -> None:
        stolen_from = self._bindings.assign(self.action.id, index, sequence)
        if stolen_from is not None:
            self._notify(f"\u201c{sequence}\u201d was taken from {stolen_from.label}")
        else:
            self._notify(f"{self.action.label}: {sequence}")

    def _on_cleared(self, index: int) -> None:
        self._bindings.remove(self.action.id, index)
        self._notify("Shortcut removed")

    def _on_added(self, sequence: str) -> None:
        self._pending = False
        stolen_from = self._bindings.assign(self.action.id, None, sequence)
        if stolen_from is not None:
            self._notify(f"\u201c{sequence}\u201d was taken from {stolen_from.label}")
        self.refresh()

    def _cancel_pending(self) -> None:
        if self._pending:
            self._pending = False
            self.refresh()

    def _on_add(self) -> None:
        self._pending = True
        self.refresh()  # the fresh chip puts itself into capture mode

    def _reset_row(self) -> None:
        self._bindings.reset(self.action.id)
        self._notify(f"{self.action.label} reset to defaults")
        self.refresh()
