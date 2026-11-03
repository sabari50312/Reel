"""The control strip: seek bar on top, transport row underneath.

The bar owns no playback state of its own. It mirrors the engine's signals and
turns clicks into engine calls, which keeps the "UI never talks to libVLC"
rule intact while making the label/knob respond instantly instead of waiting
for the next 30 Hz poll.

Layout notes:
  * everything is hand-painted widgets, so no QSS box model is involved and the
    strip's height is exactly SEEK_HIT + BUBBLE_H + BTN (48 + 40 = 88 px).
  * the time label is rich text so " / duration" can be dimmed without a
    second widget.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ..style import tokens
from .iconbutton import IconButton
from .seekbar import SEEKBAR_H, SeekBar
from .timefmt import format_time, needs_hours
from .volumebar import VolumeBar

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps vlc out of imports
    from ..engine import Player

_MARGIN_LEFT = 0
_ROW_SPACING = 8


class ControlBar(QWidget):
    open_requested = Signal()
    fullscreen_requested = Signal()
    interaction = Signal(bool)  # True while scrubbing, keeps the overlay awake

    def __init__(self, engine: "Player", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._engine = engine
        self._duration = 0
        self._position = 0
        self._has_media = False

        self.setFixedHeight(SEEKBAR_H + tokens.BTN)

        self.seek = SeekBar(self)
        self.play_btn = IconButton("play", self, tip="Play (Space)")
        self.time_label = QLabel(self)
        self.volume = VolumeBar(self)
        self.open_btn = IconButton("open", self, tip="Open file (Ctrl+O)")
        self.fs_btn = IconButton("fullscreen", self, tip="Fullscreen (F)")

        font = QFont(tokens.FONT_FAMILY)
        font.setPixelSize(tokens.FS_TIME)
        self.time_label.setFont(font)
        self.time_label.setTextFormat(Qt.TextFormat.RichText)
        self.time_label.setContentsMargins(4, 0, 0, 0)
        self.time_label.setAlignment(
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
        )

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(_ROW_SPACING)
        row.addWidget(self.play_btn)
        row.addWidget(self.time_label)
        row.addStretch(1)
        row.addWidget(self.volume)
        row.addWidget(self.open_btn)
        row.addWidget(self.fs_btn)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(_MARGIN_LEFT, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.seek)
        outer.addLayout(row)

        self._wire()
        self.sync_from_engine()

    # ------------------------------------------------------------------ wiring
    def _wire(self) -> None:
        engine = self._engine
        self.play_btn.clicked.connect(engine.toggle)
        self.open_btn.clicked.connect(self.open_requested)
        self.fs_btn.clicked.connect(self.fullscreen_requested)
        self.seek.seek_requested.connect(self._on_seek_requested)
        self.seek.scrubbing_changed.connect(self.interaction)
        self.volume.volume_changed.connect(engine.set_volume)
        self.volume.muted_changed.connect(engine.set_muted)

        engine.opened.connect(self._on_opened)
        engine.position_changed.connect(self._on_position)
        engine.state_changed.connect(self._on_state_changed)
        engine.ended.connect(self._on_ended)
        engine.volume_changed.connect(self._on_volume_changed)
        engine.muted_changed.connect(self.volume.set_muted)

    def sync_from_engine(self) -> None:
        """Seed volume/mute from the engine without emitting back at it."""
        self.volume.set_volume(self._engine.volume(), self._engine.is_muted())
        self._on_state_changed(self._engine.is_playing())

    # ------------------------------------------------------------------- slots
    def _on_opened(self, duration_ms: int, has_video: bool) -> None:  # noqa: ARG002
        self._has_media = True
        self._duration = max(0, int(duration_ms))
        self._position = 0
        self.seek.setEnabled(True)
        self.play_btn.setEnabled(True)
        self.seek.set_duration(self._duration)
        self.seek.set_position(0)
        self._refresh_label()

    def _on_position(self, position_ms: int) -> None:
        self._position = max(0, int(position_ms))
        self.seek.set_position(self._position)
        self._refresh_label()

    def _on_state_changed(self, playing: bool) -> None:
        self.play_btn.set_icon_name("pause" if playing else "play")
        self.play_btn.setToolTip("Pause (Space)" if playing else "Play (Space)")

    def _on_ended(self) -> None:
        self._position = 0
        self.seek.set_position(0)
        self._on_state_changed(False)
        self._refresh_label()

    def _on_volume_changed(self, volume: int) -> None:
        if volume != self.volume.volume_value():  # avoid fighting an in-flight drag
            self.volume.set_volume(volume, self._engine.is_muted())

    def _on_seek_requested(self, position_ms: int) -> None:
        self._position = max(0, int(position_ms))
        self.seek.set_position(self._position)  # knob follows immediately
        self._refresh_label()
        self._engine.seek_ms(self._position)

    # ------------------------------------------------------------------- views
    def _refresh_label(self) -> None:
        hours = needs_hours(self._duration)
        current = format_time(self._position, hours)
        total = format_time(self._duration, hours)
        text = f"{current}<span style='color:{tokens.TEXT_DIM}'>&nbsp;/&nbsp;{total}</span>"
        self.time_label.setText(text)

    def set_fullscreen(self, fullscreen: bool) -> None:
        self.fs_btn.set_icon_name("exit_fullscreen" if fullscreen else "fullscreen")
        self.fs_btn.setToolTip("Exit fullscreen (Esc)" if fullscreen else "Fullscreen (F)")

    def set_loaded(self, loaded: bool) -> None:
        self._has_media = loaded
        self.seek.setEnabled(loaded)
        self.play_btn.setEnabled(loaded)
        if not loaded:
            self._duration = 0
            self._position = 0
            self.seek.set_duration(0)
            self.seek.set_position(0)
            self._refresh_label()

    def refresh_volume(self) -> None:
        self.volume.set_volume(self._engine.volume(), self._engine.is_muted())
