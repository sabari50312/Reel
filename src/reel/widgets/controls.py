"""The floating control card: one row, everything in it.

Deliberately a single row (prev · play · next · 00:00 · seek · 00:00 · volume ·
audio · subtitles · speed · settings · queue · fullscreen), matching the reference
design: the player has one rounded card floating over the bottom of the video
instead of the old two-row strip plus gradient scrim.

The bar owns no playback state. It mirrors the engine's signals and turns clicks
into engine calls, which keeps "the UI never talks to libVLC" true while letting
the label and knob react instantly instead of waiting for the next poll.

Everything here is hand-painted, so the card's height is exact and no QSS box
model is involved. Buttons shrink/hide instead of squashing when the window gets
narrow: prev/next disappear first, then the volume slider, then the timecodes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QPoint, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QMouseEvent,
    QPaintEvent,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QLabel, QWidget

from ..style import tokens
from .iconbutton import IconButton
from .icons import paint_icon
from .seekbar import SeekBar
from .timefmt import format_time, needs_hours
from .volumebar import VolumeBar

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps vlc out of imports
    from ..engine import Player

_ROW_SPACING = 8
_SIDE_PAD = 14  # inside the card
_SMALL_BTN = tokens.BTN_SM  # 34
_SMALL_ICON = 20

# Window widths at which a control stops being shown. Order matters: these are
# checked independently, so the timecodes are the first thing to go.
_W_PREV_NEXT = 1020  # inert while the playlist does not exist yet
_W_VOLUME = 900
_W_TRACK_BUTTONS = 760  # audio / subtitles / speed
_W_TIMECODES = 640
_W_ANALYSE = 940


class PlayButton(QAbstractButton):
    """Primary action: a wide accent-filled chip, not an icon button.

    The accent is white right now (user's call: "white now, accent later"), so the
    glyph has to be the window colour instead of white.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._playing = False
        self.setFixedSize(tokens.PLAY_W, tokens.PLAY_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip("Play (Space)")

    def enterEvent(self, event) -> None:  # noqa: ANN001 - Qt signature
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: ANN001 - Qt signature
        self.update()
        super().leaveEvent(event)

    def set_playing(self, playing: bool) -> None:
        if playing != self._playing:
            self._playing = playing
            self.setToolTip("Pause (Space)" if playing else "Play (Space)")
            self.update()

    def is_playing(self) -> bool:
        return self._playing

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        if not self.isEnabled():
            fill = QColor(tokens.ACCENT)
            fill.setAlpha(40)
            glyph = tokens.TEXT_FAINT
        else:
            fill = QColor("#ffffff")
            glyph = tokens.ON_ACCENT
            if self.isDown():
                fill = QColor("#dfe4ea")
            elif not self.underMouse():
                fill = QColor(tokens.ACCENT)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(QRectF(self.rect()), 8.0, 8.0)

        side = 18.0
        rect = QRectF(
            (self.width() - side) / 2.0,
            (self.height() - side) / 2.0,
            side,
            side,
        )
        paint_icon(painter, "pause" if self._playing else "play", rect, glyph, 1.9)
        painter.end()


class ControlBar(QWidget):
    files_requested = Signal()  # toggle the side file browser
    fullscreen_requested = Signal()
    settings_requested = Signal()
    speed_requested = Signal()  # the window pops the speed menu under the button
    audio_requested = Signal()  # cycle audio track, window shows the toast
    subtitles_requested = Signal()  # cycle subtitle track
    menu_requested = Signal(QPoint)  # right click on the card: global position
    interaction = Signal(bool)  # True while scrubbing, keeps the overlay awake

    def __init__(self, engine: "Player", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._engine = engine
        self._duration = 0
        self._position = 0
        self._has_media = False

        self.setFixedHeight(tokens.BAR_H)
        # Opaque background is painted by us; without this the parent's gradient
        # (now gone) used to show through the card's corners.
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)

        self.seek = SeekBar(self)
        self.play_btn = PlayButton(self)

        self.files_btn = IconButton(
            "folder", self, size=_SMALL_BTN, icon_size=_SMALL_ICON, tip="Browse files (Ctrl+O)"
        )

        self.prev_btn = IconButton(
            "prev", self, size=_SMALL_BTN, icon_size=_SMALL_ICON, tip="Previous (playlist soon)"
        )
        self.next_btn = IconButton(
            "next", self, size=_SMALL_BTN, icon_size=_SMALL_ICON, tip="Next (playlist soon)"
        )
        self.queue_btn = IconButton(
            "queue", self, size=_SMALL_BTN, icon_size=_SMALL_ICON, tip="Queue (playlist soon)"
        )
        # Placed now, wired when the playlist lands (user's call).
        for placeholder in (self.prev_btn, self.next_btn, self.queue_btn):
            placeholder.setEnabled(False)

        self.elapsed_label = QLabel(self)
        self.total_label = QLabel(self)
        # Analysing a long file takes a while and the bar is the only place the
        # result shows up, so say that work is happening instead of looking idle.
        self.analysis_label = QLabel(self)
        self.analysis_label.setObjectName("AnalyseHint")
        self.analysis_label.setFixedWidth(104)
        self.analysis_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.analysis_label.hide()
        self._analysis_active = False
        self.volume = VolumeBar(self)
        self.audio_btn = IconButton(
            "waveform", self, size=_SMALL_BTN, icon_size=_SMALL_ICON, tip="Audio track (B)"
        )
        self.sub_btn = IconButton(
            "subtitles", self, size=_SMALL_BTN, icon_size=_SMALL_ICON, tip="Subtitles (V)"
        )
        self.speed_btn = IconButton(
            "speed", self, size=_SMALL_BTN, icon_size=_SMALL_ICON, tip="Playback speed (] [)"
        )
        self.settings_btn = IconButton(
            "settings", self, size=_SMALL_BTN, icon_size=_SMALL_ICON, tip="Settings (Ctrl+P)"
        )
        self.fs_btn = IconButton(
            "fullscreen", self, size=_SMALL_BTN, icon_size=_SMALL_ICON, tip="Fullscreen (F)"
        )

        time_font = QFont(tokens.FONT_MONO)
        time_font.setPixelSize(tokens.FS_TIME)
        for label, color in ((self.elapsed_label, tokens.TEXT), (self.total_label, tokens.TEXT_DIM)):
            label.setFont(time_font)
            label.setStyleSheet(f"color: {color};")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        row = QHBoxLayout(self)
        row.setContentsMargins(_SIDE_PAD, 0, _SIDE_PAD, 0)
        row.setSpacing(_ROW_SPACING)
        row.addWidget(self.files_btn)
        row.addWidget(self.prev_btn)
        row.addWidget(self.play_btn)
        row.addWidget(self.next_btn)
        row.addWidget(self.elapsed_label)
        row.addWidget(self.seek, 1)
        row.addWidget(self.total_label)
        row.addWidget(self.analysis_label)
        row.addSpacing(6)
        row.addWidget(self.volume)
        row.addWidget(self.audio_btn)
        row.addWidget(self.sub_btn)
        row.addWidget(self.speed_btn)
        row.addWidget(self.settings_btn)
        row.addWidget(self.queue_btn)
        row.addWidget(self.fs_btn)

        self._wire()
        self.sync_from_engine()
        self._refresh_labels()

    # ------------------------------------------------------------------ wiring
    def _wire(self) -> None:
        engine = self._engine
        self.files_btn.clicked.connect(self.files_requested)
        self.play_btn.clicked.connect(engine.toggle)
        self.prev_btn.clicked.connect(lambda: None)  # inert until the playlist exists
        self.next_btn.clicked.connect(lambda: None)
        self.queue_btn.clicked.connect(lambda: None)
        self.settings_btn.clicked.connect(self.settings_requested)
        self.fs_btn.clicked.connect(self.fullscreen_requested)
        self.speed_btn.clicked.connect(self.speed_requested)
        self.audio_btn.clicked.connect(self.audio_requested)
        self.sub_btn.clicked.connect(self.subtitles_requested)
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
        self._refresh_labels()

    def _on_position(self, position_ms: int) -> None:
        self._position = max(0, int(position_ms))
        self.seek.set_position(self._position)
        self._refresh_labels()

    def _on_state_changed(self, playing: bool) -> None:
        self.play_btn.set_playing(playing)

    def _on_ended(self) -> None:
        self._position = 0
        self.seek.set_position(0)
        self._on_state_changed(False)
        self._refresh_labels()

    def _on_volume_changed(self, volume: int) -> None:
        if volume != self.volume.volume_value():  # avoid fighting an in-flight drag
            self.volume.set_volume(volume, self._engine.is_muted())

    def _on_seek_requested(self, position_ms: int) -> None:
        self._position = max(0, int(position_ms))
        self.seek.set_position(self._position)  # knob follows immediately
        self._refresh_labels()
        self._engine.seek_ms(self._position)

    # ------------------------------------------------------------------- views
    def _refresh_labels(self) -> None:
        hours = needs_hours(self._duration)
        self.elapsed_label.setText(format_time(self._position, hours))
        self.total_label.setText(format_time(self._duration, hours))
        # Fixed width from the widest possible text stops the seek bar twitching
        # every time a digit changes.
        metrics = QFontMetrics(self.elapsed_label.font())
        width = metrics.horizontalAdvance("00:00:00" if hours else "00:00") + 4
        for label in (self.elapsed_label, self.total_label):
            label.setFixedWidth(width)

    def set_audio_map(self, levels: list[int] | None) -> None:
        """Loudness bands for the seek bar; None while a file is still analysed."""
        self.seek.set_levels(levels)

    def set_fullscreen(self, fullscreen: bool) -> None:
        self.fs_btn.set_icon_name("exit_fullscreen" if fullscreen else "fullscreen")
        self.fs_btn.setToolTip("Exit fullscreen (Esc)" if fullscreen else "Fullscreen (F)")

    def set_analysis(self, fraction: float | None) -> None:
        """`None` hides the indicator; 0..1 shows decoding progress."""
        self._analysis_active = fraction is not None
        if fraction is None:
            self.analysis_label.hide()
            return
        percent = max(0, min(100, int(round(fraction * 100))))
        self.analysis_label.setText(f"Analysing {percent}%")
        self.analysis_label.setVisible(self.width() >= _W_ANALYSE)

    def set_loaded(self, loaded: bool) -> None:
        self._has_media = loaded
        self.seek.setEnabled(loaded)
        self.play_btn.setEnabled(loaded)
        if not loaded:
            self._duration = 0
            self._position = 0
            self.seek.set_duration(0)
            self.seek.set_position(0)
            self._refresh_labels()

    def refresh_volume(self) -> None:
        self.volume.set_volume(self._engine.volume(), self._engine.is_muted())

    def speed_button(self) -> IconButton:
        """Anchor for the speed popup."""
        return self.speed_btn

    # ---------------------------------------------------------------- painting
    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        card = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        # Translucent card. The strip is its own layered window over the native
        # video HWND, so this alpha is real: frames (and the subtitles libVLC
        # draws) show through. The fill darkens towards the bottom edge, where the
        # timecodes and the seek rail sit - flat 75% over a bright scene turned
        # them into mush.
        fill = QLinearGradient(card.topLeft(), card.bottomLeft())
        fill.setColorAt(0.0, QColor(tokens.BAR_BG))
        fill.setColorAt(1.0, QColor(tokens.BAR_BG_END))
        painter.setPen(QPen(QColor(tokens.BAR_BORDER), 1.0))
        painter.setBrush(QBrush(fill))
        painter.drawRoundedRect(card, tokens.BAR_RADIUS, tokens.BAR_RADIUS)
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        # Empty space in the card behaves like empty space on the video.
        if event.button() == Qt.MouseButton.LeftButton:
            self._engine.toggle()
        elif event.button() == Qt.MouseButton.RightButton:
            self.menu_requested.emit(event.globalPosition().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    # -------------------------------------------------------------- responsive
    def resizeEvent(self, event) -> None:  # noqa: ANN001 - Qt signature
        width = self.width()
        for widget, threshold in (
            (self.prev_btn, _W_PREV_NEXT),
            (self.next_btn, _W_PREV_NEXT),
            (self.volume, _W_VOLUME),
            (self.audio_btn, _W_TRACK_BUTTONS),
            (self.sub_btn, _W_TRACK_BUTTONS),
            (self.speed_btn, _W_TRACK_BUTTONS),
            (self.elapsed_label, _W_TIMECODES),
            (self.total_label, _W_TIMECODES),
        ):
            widget.setVisible(width >= threshold)
        self.analysis_label.setVisible(self._analysis_active and width >= _W_ANALYSE)
        super().resizeEvent(event)
