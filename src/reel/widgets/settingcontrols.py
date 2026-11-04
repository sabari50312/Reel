"""Controls for the settings pages.

Three small widgets, each chosen over its Qt equivalent for a reason:

  * `ToggleSwitch` - a 12 px QCheckBox reads as a bullet list, not as a switch.
    Painted, so it matches the rest of the UI at any DPI.
  * `Segmented` - an exclusive row of chips. For two or three options this is
    one click, where a QComboBox is two plus a popup.
  * `Stepper` - `[-] 2.6 s [+]`. A QSpinBox brings a text cursor, arrow buttons,
    a keyboard grab and locale-dependent parsing; none of that is wanted here.
  * `TextRow` - a plain QLineEdit, because an API key or a language list has to
    be pasted or typed, and a custom control would only get in the way.

The rows below bind one control to one stored setting in both directions. The
store is the single source of truth: a row never keeps its own copy.
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
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..settings import Settings
from ..style import tokens
from .elided_label import ElidedLabel


def _blend(first: QColor, second: QColor, t: float) -> QColor:
    return QColor.fromRgbF(
        first.redF() + (second.redF() - first.redF()) * t,
        first.greenF() + (second.greenF() - first.greenF()) * t,
        first.blueF() + (second.blueF() - first.blueF()) * t,
    )


class ToggleSwitch(QAbstractButton):
    """A pill switch. `setChecked` jumps without animating (store sync, not input)."""

    WIDTH = 44
    HEIGHT = 24
    KNOB = 18

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self._position = 0.0  # 0 = off, 1 = on
        self._slide = QPropertyAnimation(self, b"position", self)
        self._slide.setDuration(tokens.GROW_MS)
        self._slide.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)

    def _get_position(self) -> float:
        return self._position

    def _set_position(self, value: float) -> None:
        self._position = float(value)
        self.update()

    position = Property(float, _get_position, _set_position)

    def _animate(self, checked: bool) -> None:
        self._slide.stop()
        self._slide.setStartValue(self._position)
        self._slide.setEndValue(1.0 if checked else 0.0)
        self._slide.start()

    def setChecked(self, checked: bool) -> None:  # noqa: N802 - Qt spelling
        blocked = self.blockSignals(True)
        super().setChecked(checked)
        self.blockSignals(blocked)
        self._slide.stop()
        self._position = 1.0 if checked else 0.0
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt spelling
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = self._position

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_blend(QColor("#2a3138"), QColor(tokens.TEXT), t))
        painter.drawRoundedRect(QRectF(self.rect()), self.HEIGHT / 2, self.HEIGHT / 2)

        travel = self.WIDTH - self.KNOB - 6
        painter.setBrush(_blend(QColor(tokens.TEXT_FAINT), QColor(tokens.BG), t))
        painter.drawEllipse(QRectF(3 + travel * t, 3, self.KNOB, self.KNOB))


class Segmented(QWidget):
    """Exclusive chips: one click instead of a combo box's two plus a popup."""

    changed = Signal(str)

    def __init__(
        self, options: list[tuple[str, str]], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[str, QPushButton] = {}

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        for value, label in options:
            button = QPushButton(label, self)
            button.setObjectName("Segment")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(lambda _checked=False, v=value: self.changed.emit(v))
            self._group.addButton(button)
            self._buttons[value] = button
            layout.addWidget(button)

    def value(self) -> str:
        for value, button in self._buttons.items():
            if button.isChecked():
                return value
        return ""

    def set_value(self, value: str) -> None:
        button = self._buttons.get(value)
        if button is not None:
            button.setChecked(True)  # the exclusive group clears the others


class Stepper(QWidget):
    """`[-] 2.6 s [+]`. Steps in display units; the row converts to stored units."""

    changed = Signal(float)

    def __init__(
        self,
        minimum: float,
        maximum: float,
        step: float,
        decimals: int = 0,
        suffix: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._minimum = float(minimum)
        self._maximum = float(maximum)
        self._step = float(step)
        self._decimals = decimals
        self._suffix = suffix
        self._value = float(minimum)

        self._minus = self._button("\u2212")
        self._plus = self._button("+")
        self._label = QLabel(self)
        self._label.setObjectName("StepValue")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setMinimumWidth(64)

        self._minus.clicked.connect(lambda: self._nudge(-1))
        self._plus.clicked.connect(lambda: self._nudge(1))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self._minus)
        layout.addWidget(self._label)
        layout.addWidget(self._plus)
        self._paint()

    def _button(self, text: str) -> QPushButton:
        button = QPushButton(text, self)
        button.setObjectName("StepBtn")
        button.setFixedSize(26, 26)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return button

    def _paint(self) -> None:
        self._label.setText(f"{self._value:.{self._decimals}f}{self._suffix}")
        self._minus.setEnabled(self._value > self._minimum + 1e-9)
        self._plus.setEnabled(self._value < self._maximum - 1e-9)

    def value(self) -> float:
        return self._value

    def set_value(self, value: float) -> None:
        self._value = min(self._maximum, max(self._minimum, round(float(value), 3)))
        self._paint()

    def _nudge(self, direction: int) -> None:
        before = self._value
        self.set_value(self._value + direction * self._step)
        if self._value != before:
            self.changed.emit(self._value)


class SettingRow(QWidget):
    """Title (and a dim hint) on the left, control on the right."""

    def search_title(self) -> str:
        return self._title

    def search_hint(self) -> str:
        return self._hint

    def __init__(
        self,
        title: str,
        hint: str,
        control: QWidget,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._title = title
        self._hint = hint
        name = ElidedLabel(title, self)
        name.setObjectName("RowTitle")
        name.setMinimumWidth(92)

        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(2)
        text.addWidget(name)
        if hint:
            # Hints are tooltips, not a second line of text: subtext made rows
            # 53px or 151px tall depending on the sentence, which is exactly the
            # ragged rhythm the reference layout avoids.
            name.setToolTip(hint)
            self.setToolTip(hint)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 6)
        layout.setSpacing(12)
        layout.addLayout(text, 1)
        layout.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setMinimumHeight(40)


class ToggleRow(SettingRow):
    def __init__(
        self,
        settings: Settings,
        key: str,
        title: str,
        hint: str = "",
        parent: QWidget | None = None,
    ) -> None:
        self._settings = settings
        self._key = key
        self._switch = ToggleSwitch(parent)
        self._switch.setChecked(bool(settings.get(key)))
        self._switch.toggled.connect(self._store)
        super().__init__(title, hint, self._switch, parent)
        settings.changed.connect(self._sync)

    def _store(self, checked: bool) -> None:
        self._settings.set(self._key, bool(checked))

    def _sync(self, key: str) -> None:
        if key == self._key:
            self._switch.setChecked(bool(self._settings.get(self._key)))


class SegmentRow(SettingRow):
    def __init__(
        self,
        settings: Settings,
        key: str,
        title: str,
        options: list[tuple[str, str]],
        hint: str = "",
        parent: QWidget | None = None,
    ) -> None:
        self._settings = settings
        self._key = key
        self._segmented = Segmented(options, parent)
        self._segmented.set_value(str(settings.get(key)))
        self._segmented.changed.connect(self._store)
        super().__init__(title, hint, self._segmented, parent)
        settings.changed.connect(self._sync)

    def _store(self, value: str) -> None:
        self._settings.set(self._key, value)

    def _sync(self, key: str) -> None:
        if key == self._key:
            self._segmented.set_value(str(self._settings.get(self._key)))


class StepperRow(SettingRow):
    """`scale` converts the stored integer into the displayed unit (ms -> s)."""

    def __init__(
        self,
        settings: Settings,
        key: str,
        title: str,
        minimum: int,
        maximum: int,
        step: int,
        decimals: int = 0,
        suffix: str = "",
        scale: int = 1,
        hint: str = "",
        parent: QWidget | None = None,
    ) -> None:
        self._settings = settings
        self._key = key
        self._scale = scale
        self._stepper = Stepper(
            minimum / scale, maximum / scale, step / scale, decimals, suffix, parent
        )
        self._stepper.set_value(int(settings.get(key)) / scale)
        self._stepper.changed.connect(self._store)
        super().__init__(title, hint, self._stepper, parent)
        settings.changed.connect(self._sync)

    def _store(self, displayed: float) -> None:
        self._settings.set(self._key, int(round(displayed * self._scale)))

    def _sync(self, key: str) -> None:
        if key == self._key:
            self._stepper.set_value(int(self._settings.get(self._key)) / self._scale)


class TextRow(SettingRow):
    """A single-line text setting - API keys, language lists, paths.

    Committed on every keystroke like the other rows (the store debounces the
    disk write), so there is no lost edit when the window is closed by clicking
    somewhere else.
    """

    def __init__(
        self,
        settings: Settings,
        key: str,
        title: str,
        hint: str = "",
        placeholder: str = "",
        width: int = 170,
        parent: QWidget | None = None,
    ) -> None:
        self._settings = settings
        self._key = key
        edit = QLineEdit(parent)
        edit.setObjectName("RowEdit")
        edit.setFixedWidth(width)
        edit.setFixedHeight(28)  # same visual height as the steppers and segments
        if placeholder:
            edit.setPlaceholderText(placeholder)
        edit.setText(str(settings.get(key)))
        edit.textChanged.connect(self._store)
        super().__init__(title, hint, edit, parent)
        self._edit = edit
        settings.changed.connect(self._sync)

    def _store(self, text: str) -> None:
        if text != str(self._settings.get(self._key)):
            self._settings.set(self._key, text)

    def _sync(self, key: str) -> None:
        if key != self._key:
            return
        value = str(self._settings.get(self._key))
        if value != self._edit.text():
            self._edit.blockSignals(True)
            self._edit.setText(value)
            self._edit.blockSignals(False)


__all__ = [
    "Segmented",
    "SegmentRow",
    "SettingRow",
    "Stepper",
    "StepperRow",
    "TextRow",
    "ToggleRow",
    "ToggleSwitch",
]
