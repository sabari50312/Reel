"""Settings as a right-hand sidebar, in tabs.

Why a sidebar and not a floating window: the player keeps its context, the panel
sits where the reference UI puts panels, and the audio/subtitle controls live
next to the settings that belong to them instead of in a separate dialog.

Layout: a flush-right surface (same language as the left file drawer), a tab
strip along the top, and one scrolling column underneath. Tabs are

  Settings   - Keybindings / Playback / Interface (sub-tabs, so the strip stays
               three items wide rather than eight)
  Audio      - audio tracks, sync delay
  Subtitles  - subtitle tracks, delay, online search

Everything applies live through `Settings.changed`; there is no OK button,
because hearing the result *is* the confirmation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QObject,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    QEvent,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QHideEvent,
    QIcon,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QLineEdit,
    QButtonGroup,
    QSlider,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import shortcuts
from .keybindings_ui import ActionRow
from .settings import Settings
from .style import tokens
from .widgets.iconbutton import IconButton
from .widgets.icons import paint_icon
from .widgets.syncstrip import SyncStrip
from .widgets.settingcontrols import SegmentRow, StepperRow, TextRow, ToggleRow

def _glyph(name: str) -> QIcon:
    """A tab icon at 18px: same painted geometry as the bar, so nothing drifts."""
    pixmap = QPixmap(18 * 2, 18 * 2)
    pixmap.setDevicePixelRatio(2.0)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    paint_icon(painter, name, QRectF(0, 0, 18, 18), tokens.TEXT_LABEL)
    painter.end()
    return QIcon(pixmap)


TAB_SETTINGS = "settings"
TAB_AUDIO = "audio"
TAB_SUBTITLES = "subtitles"
TABS: tuple[tuple[str, str, str], ...] = (
    (TAB_SETTINGS, "Settings", "sliders"),
    (TAB_AUDIO, "Audio", "waveform"),
    (TAB_SUBTITLES, "Subtitles", "subtitles"),
)

_SLIDE_MS = 170
_SLIDE_PX = 10
_FADE_MS = 90  # open/close fade: long enough to read as motion, short enough to feel instant
_DELAY_LIMIT_MS = 600_000

_SUB_LABELS = ("Keybindings", "Playback", "Interface")  # settings-tab sub-pages, in order
_SECTION_GAP = 20  # space above a section rule
_ROW_GAP = 6  # between rows inside one section
_ROW_PAD = 6  # vertical padding inside a row
_SWITCH_MS = 150  # tab slide: quick enough to not feel like waiting
_SWITCH_PX = 10  # how far a new page travels in
_SLIDER_RANGE_MS = 10_000  # the reference's -10 s .. +10 s slider


class _Card(QWidget):
    """The sidebar's surface: rounded on the inner (left) edge, flush on the right."""

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        radius = tokens.PANEL_RADIUS
        # Offset to the right so the right-hand corners fall outside the widget and
        # the panel looks attached to the window edge instead of floating.
        body = QRectF(0.0, 0.0, self.width() + radius, self.height())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(tokens.PANEL_BG))
        painter.drawRoundedRect(body, radius, radius)
        painter.setPen(QPen(QColor(tokens.PANEL_BORDER), 1.0))
        painter.drawLine(1, 0, 1, self.height())
        painter.end()



class _TabButton(QPushButton):
    def __init__(self, text: str, icon_name: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("PanelTab")
        if icon_name:
            self.setIcon(_glyph(icon_name))
            self.setIconSize(QSize(16, 16))
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)


class TrackList(QWidget):
    """Radio list of tracks; empty means "nothing to pick", which is said plainly."""

    chosen = Signal(int)

    def __init__(self, empty_text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._empty_text = empty_text
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(2)
        self._buttons: list[QPushButton] = []
        self._note: QLabel | None = None
        self.set_tracks([], -1)

    def set_tracks(self, tracks: list[tuple[int, str]], current: int) -> None:
        for button in self._buttons:
            self._group.removeButton(button)
            button.setParent(None)
            button.deleteLater()
        self._buttons = []
        if self._note is not None:
            self._note.setParent(None)
            self._note.deleteLater()
            self._note = None

        if not tracks:
            self._note = QLabel(self._empty_text, self)
            self._note.setObjectName("RowHint")
            self._layout.addWidget(self._note)
            return

        for track_id, label in tracks:
            button = QPushButton(self._short(label), self)
            button.setObjectName("TrackChoice")
            button.setCheckable(True)
            button.setChecked(track_id == current)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, tid=track_id: self.chosen.emit(tid))
            self._group.addButton(button)
            self._layout.addWidget(button)
            self._buttons.append(button)

    @staticmethod
    def _short(label: str) -> str:
        return label if len(label) <= 42 else label[:40] + "\u2026"


class DelayRow(QWidget):
    """A labelled value field plus the reference-style slider under it.

    The slider is the coarse control (10 s either way, which is the whole useful
    range for sync) and the field shows the exact value; the small buttons that
    used to sit here are gone because a slider is both faster and quieter.
    """

    def __init__(
        self,
        title: str,
        settings: Settings,
        step_key: str,
        read: Callable[[], int],
        write: Callable[[int], None],
        hint: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._title = title
        self._hint = hint
        self._settings = settings
        self._step_key = step_key
        self._read = read
        self._write = write

        name = QLabel(title, self)
        name.setObjectName("RowTitle")
        name.setMinimumWidth(92)

        self._value = QLabel("", self)
        self._value.setObjectName("DelayValue")
        self._value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._value.setFixedWidth(112)

        reset = QPushButton("Reset", self)
        reset.setObjectName("RowReset")
        reset.setCursor(Qt.CursorShape.PointingHandCursor)
        reset.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        reset.clicked.connect(lambda: self._set(0))

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        top.addWidget(name, 1)
        top.addWidget(self._value)
        top.addWidget(reset)

        self._slider = QSlider(Qt.Orientation.Horizontal, self)
        self._slider.setObjectName("DelaySlider")
        self._slider.setRange(-_SLIDER_RANGE_MS, _SLIDER_RANGE_MS)
        self._slider.setSingleStep(50)
        self._slider.setPageStep(250)
        self._slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._slider.valueChanged.connect(self._on_slider)

        scale = QHBoxLayout()
        scale.setContentsMargins(2, 0, 2, 0)
        for text, align in (
            (f"-{_SLIDER_RANGE_MS // 1000} s", Qt.AlignmentFlag.AlignLeft),
            ("0 s", Qt.AlignmentFlag.AlignHCenter),
            (f"+{_SLIDER_RANGE_MS // 1000} s", Qt.AlignmentFlag.AlignRight),
        ):
            label = QLabel(text, self)
            label.setObjectName("RowHint")
            label.setAlignment(align | Qt.AlignmentFlag.AlignVCenter)
            scale.addWidget(label, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, _ROW_PAD, 0, _ROW_PAD)
        layout.setSpacing(6)
        layout.addLayout(top)
        layout.addWidget(self._slider)
        layout.addLayout(scale)
        if hint:
            note = QLabel(hint, self)
            note.setObjectName("RowHint")
            note.setWordWrap(True)
            layout.addWidget(note)
        self.refresh()

    def search_title(self) -> str:
        return self._title

    def search_hint(self) -> str:
        return self._hint

    def refresh(self) -> None:
        milliseconds = self._read()
        self._value.setText(f"{milliseconds / 1000.0:+.3f} s" if milliseconds else "0.000 s")
        self._slider.blockSignals(True)
        self._slider.setValue(max(-_SLIDER_RANGE_MS, min(_SLIDER_RANGE_MS, milliseconds)))
        self._slider.blockSignals(False)

    def _on_slider(self, value: int) -> None:
        step = max(1, int(self._settings.get(self._step_key)))
        self._set(int(round(value / step)) * step)

    def _set(self, milliseconds: int) -> None:
        self._write(max(-_DELAY_LIMIT_MS, min(_DELAY_LIMIT_MS, int(milliseconds))))
        self.refresh()


class _SlideHost(QWidget):
    """Holds a page stack just off its resting spot, so a tab change can slide it in.

    Deliberately *geometry*-animated, not faded: the card already carries a
    QGraphicsOpacityEffect for the open/close fade, and an opacity effect on a
    stack nested inside it makes Qt render one effect's source while the other
    is still painting. That produced a flood of `QPainter::translate: Painter
    not active` / `QPainter::begin: A paint device can only be painted by one
    painter at a time` and the panel failed to show at all.

    The stack is parented here without a layout so `pos` can be animated; the
    host keeps it sized in `resizeEvent`.
    """

    def __init__(self, child: QWidget, parent: QWidget) -> None:
        super().__init__(parent)
        self._child = child
        child.setParent(self)
        self._offset = 0
        self._anim = QPropertyAnimation(self, b"offset", self)
        self._anim.setDuration(_SWITCH_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def child(self) -> QWidget:
        return self._child

    def _get_offset(self) -> int:
        return self._offset

    def _set_offset(self, value: int) -> None:
        self._offset = int(value)
        self._child.move(self._offset, 0)

    offset = Property(int, _get_offset, _set_offset)

    def resizeEvent(self, event) -> None:  # noqa: ANN001 - Qt signature
        self._child.setGeometry(self._offset, 0, self.width(), self.height())
        super().resizeEvent(event)

    def slide_in(self) -> None:
        self._anim.stop()
        self._anim.setStartValue(_SWITCH_PX)
        self._anim.setEndValue(0)
        self._anim.start()


@dataclass(frozen=True)
class _Entry:
    """One searchable row: what it says, where it lives, and what to flash."""

    tab: str
    subtab: int | None
    title: str
    hint: str
    widget: QWidget

    def path(self) -> str:
        tab_label = next((label for key, label, _icon in TABS if key == self.tab), self.tab)
        if self.subtab is None:
            return tab_label
        return f"{tab_label} › {_SUB_LABELS[self.subtab]}"


class SettingsPanel(QWidget):
    visibility_changed = Signal(bool)
    search_requested = Signal()
    load_subtitle_requested = Signal()
    menu_requested = Signal(QPoint)
    seek_requested = Signal(int)

    def __init__(self, settings: Settings, host: QWidget, engine=None) -> None:  # noqa: ANN001
        super().__init__(host, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self._host = host
        self._settings = settings
        self._engine = engine
        self._bindings = shortcuts.KeyBindings(settings)
        self._rows: list[ActionRow] = []
        self._closing_slide = False

        self.setObjectName("SettingsPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # Focus policy: the panel activates on open and closes as soon as it loses
        # activation. A panel that stayed open while unfocused could slip behind
        # the video window and look like it had vanished; closing is also what a
        # settings dialog should do. Player shortcuts keep working while it is
        # focused because the router accepts events from windows parented to the
        # player (see shortcuts._accepts).

        self._card = _Card(self)
        self._effect = QGraphicsOpacityEffect(self._card)
        self._effect.setOpacity(0.0)
        self._card.setGraphicsEffect(self._effect)

        self._stack = QStackedWidget(self._card)
        self._page_host = _SlideHost(self._stack, self._card)
        self._tab_bar = QWidget(self._card)
        self._tab_bar.setObjectName("PanelTabs")
        self._tabs = QHBoxLayout(self._tab_bar)
        self._tabs.setContentsMargins(10, 6, 8, 0)
        self._tabs.setSpacing(2)
        self._tab_group = QButtonGroup(self)
        self._tab_group.setExclusive(True)
        self._tab_buttons: dict[str, _TabButton] = {}
        for key, label, icon_name in TABS:
            button = _TabButton(label, icon_name)
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, name=key: self.open_tab(name))
            self._tab_group.addButton(button)
            self._tabs.addWidget(button)
            self._tab_buttons[key] = button
        self._tabs.addStretch(1)
        self._close_btn = IconButton("close", self._tab_bar, size=28, icon_size=16, tip="Close (Esc)")
        self._close_btn.clicked.connect(self.close_panel)
        self._tabs.addWidget(self._close_btn)

        self._stack.addWidget(self._build_settings_tab())
        self._stack.addWidget(self._build_audio_tab())
        self._stack.addWidget(self._build_subtitles_tab())

        # Search results live as one more page: filtering rows in place would
        # leave gaps and half-sections, and cross-tab hits need somewhere to go.
        self._results_page = self._build_results_page()
        self._results_index = self._stack.count()
        self._stack.addWidget(self._results_page)

        self._search_bar = self._build_search_bar()
        self._search_bar.hide()
        self._search_origin = 0
        self._search_subtab: int | None = None
        self._last_tab_index = 0

        body = QVBoxLayout(self._card)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._tab_bar)
        body.addWidget(self._search_bar)
        body.addWidget(self._page_host, 1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._card)

        self._slide = QPropertyAnimation(self, b"pos")
        self._slide.setDuration(_SLIDE_MS)
        self._slide.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._slide.finished.connect(self._on_slide_finished)
        self._fade = QPropertyAnimation(self._effect, b"opacity")
        self._fade.setDuration(_FADE_MS)

        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.setInterval(4000)
        self._status_timer.timeout.connect(lambda: self._status.setText(""))

        self._rest = QRect()
        self._current = TAB_SETTINGS
        settings.changed.connect(self._on_changed)
        # The panel is its own top-level window, so nothing moves it when the
        # player is resized, maximised or goes fullscreen: without this filter it
        # kept the absolute position it was opened at.
        host.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:  # noqa: ANN001 - Qt signature
        if watched is getattr(self, "_search_field", None) and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self.close_search()
                return True
        if watched is self._host:
            kind = event.type()
            if kind in (QEvent.Type.Move, QEvent.Type.Resize):
                if self.isVisible():
                    if self._slide.state() == QPropertyAnimation.State.Running:
                        self._rest = self._target_rect()
                    else:
                        self.sync_geometry()
            elif kind in (QEvent.Type.Hide, QEvent.Type.Close):
                self.hide()
        return super().eventFilter(watched, event)

    # ------------------------------------------------------------------ pages
    def _build_settings_tab(self) -> QWidget:
        page = QWidget(self)
        page.setObjectName("SettingsPage")

        sub = QHBoxLayout()
        sub.setContentsMargins(14, 0, 14, 6)
        sub.setSpacing(4)
        self._sub_group = QButtonGroup(page)
        self._sub_group.setExclusive(True)
        self._sub_stack = QStackedWidget(page)
        self._sub_host = _SlideHost(self._sub_stack, page)
        for index, (label, icon, builder) in enumerate(
            (
                ("Keybindings", "key", self._build_keyboard_page),
                ("Playback", "play", self._build_playback_page),
                ("Interface", "layout", self._build_interface_page),
            )
        ):
            button = _TabButton(label, icon)
            button.clicked.connect(lambda _checked=False, i=index: self._show_sub_tab(i))
            self._sub_group.addButton(button)
            sub.addWidget(button)
            self._sub_stack.addWidget(builder())
            if index == 0:
                button.setChecked(True)
        sub.addStretch(1)

        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(sub)
        layout.addWidget(self._sub_host, 1)
        return page

    def _build_keyboard_page(self) -> QWidget:
        holder = QWidget(self)
        holder.setObjectName("RowsHolder")
        rows = QVBoxLayout(holder)
        rows.setContentsMargins(14, 5, 14, 5)
        rows.setSpacing(2)
        for action in shortcuts.ACTIONS:
            row = ActionRow(action, self._bindings, self._show_status, holder)
            rows.addWidget(row)
            self._rows.append(row)
        rows.addStretch(1)

        self._status = QLabel("", self)
        self._status.setObjectName("StatusNote")

        reset_all = QPushButton("Reset all shortcuts", self)
        reset_all.setObjectName("ResetAll")
        reset_all.setCursor(Qt.CursorShape.PointingHandCursor)
        reset_all.clicked.connect(self._reset_all)

        footer = QHBoxLayout()
        footer.setContentsMargins(14, 6, 14, 10)
        footer.setSpacing(10)
        footer.addWidget(reset_all)
        footer.addWidget(self._status, 1)

        page = QWidget(self)
        page.setObjectName("SettingsPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._scroll(holder), 1)
        layout.addLayout(footer)
        return page

    def _build_page(self, title: str, hint: str, sections: list[tuple[str, list[QWidget]]]) -> QWidget:
        page = QWidget(self)
        page.setObjectName("SettingsPage")

        holder = QWidget(page)
        holder.setObjectName("RowsHolder")
        rows = QVBoxLayout(holder)
        rows.setContentsMargins(16, 10, 16, 16)
        rows.setSpacing(_ROW_GAP)
        for index, (section, widgets) in enumerate(sections):
            label = QLabel(section, holder)
            label.setObjectName("SectionTitle")
            if index:
                rows.addSpacing(_SECTION_GAP - _ROW_GAP)
                rule = QFrame(holder)
                rule.setObjectName("SectionRule")
                rule.setFrameShape(QFrame.Shape.NoFrame)
                rule.setFixedHeight(1)
                rows.addWidget(rule)
                rows.addSpacing(8)
            rows.addWidget(label)
            for widget in widgets:
                widget.setProperty("searchSection", section)
                rows.addWidget(widget)
        rows.addStretch(1)

        heading = QLabel(title, page)
        heading.setObjectName("PageTitle")
        note = QLabel(hint, page)
        note.setObjectName("PageHint")
        note.setWordWrap(True)

        header = QVBoxLayout()
        header.setContentsMargins(14, 6, 14, 4)
        header.setSpacing(3)
        header.addWidget(heading)
        header.addWidget(note)

        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(header)
        layout.addWidget(self._scroll(holder), 1)
        return page

    def _scroll(self, holder: QWidget) -> QScrollArea:
        scroll = QScrollArea(self)
        scroll.setObjectName("RowsScroll")
        scroll.setWidget(holder)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return scroll

    def _build_playback_page(self) -> QWidget:
        settings = self._settings
        return self._build_page(
            "Playback",
            "Everything applies immediately, mid-playback.",
            [
                (
                    "Playback",
                    [
                        ToggleRow(
                            settings,
                            "play.autoplay",
                            "Start playing when a file opens",
                            hint="Off: the first frame is shown, paused.",
                        ),
                        StepperRow(settings, "play.step_s", "Seek step", 1, 60, 1, suffix=" s", hint="Left / Right."),
                        StepperRow(settings, "play.short_step_s", "Fine seek step", 1, 60, 1, suffix=" s", hint="J / L."),
                        StepperRow(
                            settings, "play.long_step_s", "Big seek step", 5, 300, 5, suffix=" s",
                            hint="Shift + Left / Right.",
                        ),
                    ],
                ),
                (
                    "Resume",
                    [
                        SegmentRow(
                            settings,
                            "files.resume",
                            "Continue where I stopped",
                            [("always", "Always"), ("ask", "Ask me"), ("off", "Never")],
                            hint="A position is kept per file. The last 10 seconds count as "
                            "finished and start over.",
                        ),
                    ],
                ),
            ],
        )

    def _build_interface_page(self) -> QWidget:
        settings = self._settings
        return self._build_page(
            "Interface",
            "How the player looks and responds.",
            [
                (
                    "Overlay",
                    [
                        StepperRow(
                            settings,
                            "ui.overlay_delay_ms",
                            "Hide the controls after",
                            600,
                            12000,
                            200,
                            decimals=1,
                            suffix=" s",
                            scale=1000,
                            hint="Counted from the last mouse move or key press.",
                        ),
                    ],
                ),
                (
                    "Mouse",
                    [
                        SegmentRow(
                            settings,
                            "input.wheel",
                            "Scroll wheel over the video",
                            [("volume", "Volume"), ("seek", "Seek"), ("ignore", "Nothing")],
                        ),
                        SegmentRow(
                            settings,
                            "input.click",
                            "Click on the video",
                            [("play_pause", "Play / pause"), ("none", "Nothing")],
                        ),
                        SegmentRow(
                            settings,
                            "input.double_click",
                            "Double-click on the video",
                            [("fullscreen", "Fullscreen"), ("none", "Nothing")],
                        ),
                    ],
                ),
                ("Window", [ToggleRow(settings, "ui.always_on_top", "Keep the player above other windows")]),
            ],
        )

    def _build_audio_tab(self) -> QWidget:
        settings = self._settings
        # Describes the map drawn behind the seek bar (bands and how many have sound).
        self.map_note = QLabel("Sound map builds when a file opens", self)
        self.map_note.setObjectName("RowHint")
        self.map_note.setWordWrap(True)
        self.audio_tracks = TrackList("No audio in this file", self)
        self.audio_tracks.chosen.connect(self._on_audio_chosen)
        self.audio_delay = DelayRow(
            "Sync delay",
            settings,
            "audio.step_ms",
            lambda: self._engine.audio_delay() if self._engine else 0,
            lambda ms: self._engine.set_audio_delay(ms) if self._engine else None,
            hint=". delays the sound, , advances it, Shift+. resets.",
            parent=self,
        )
        return self._build_page(
            "Audio",
            "Pick the track here; B cycles them without opening the panel.",
            [
                ("Tracks", [self.audio_tracks]),
                (
                    "Sound map",
                    [
                        self.map_note,
                    ],
                ),
                (
                    "Sync",
                    [
                        self.audio_delay,
                        StepperRow(settings, "audio.step_ms", "Delay step", 10, 500, 10, suffix=" ms"),
                    ],
                ),
            ],
        )

    def _sync_hint(self) -> QLabel:
        """One line under the sync strip: the lanes read as decoration otherwise."""
        label = QLabel(
            "White bars are subtitle lines, green is sound. Drag to scrub; Z / X shift the subtitles.",
            self,
        )
        label.setObjectName("RowHint")
        label.setWordWrap(True)
        label.setToolTip(
            "The two lanes show the same 20 seconds of the file. Park near a line, "
            "then nudge the delay until its bar sits over the speech."
        )
        return label

    def _build_subtitles_tab(self) -> QWidget:
        settings = self._settings
        self.subtitle_tracks = TrackList("No subtitles in this file", self)
        self.subtitle_tracks.chosen.connect(self._on_subtitle_chosen)
        self.subtitle_delay = DelayRow(
            "Delay",
            settings,
            "sub.step_ms",
            lambda: self._engine.subtitle_delay() if self._engine else 0,
            lambda ms: self._engine.set_subtitle_delay(ms) if self._engine else None,
            hint="Z delays the subtitles, X advances them, Shift+Z resets.",
            parent=self,
        )

        search = QPushButton("Search online\u2026", self)
        search.setObjectName("PromptPrimary")
        search.setFixedHeight(34)
        search.setCursor(Qt.CursorShape.PointingHandCursor)
        search.clicked.connect(self.search_requested)
        load = QPushButton("Load a subtitle file\u2026", self)
        load.setObjectName("PromptSecondary")
        load.setFixedHeight(34)
        load.setCursor(Qt.CursorShape.PointingHandCursor)
        load.clicked.connect(self.load_subtitle_requested)

        self._style_note = QLabel("Changes reload the file at the same position.", self)
        self._style_note.setObjectName("RowHint")
        self._style_note.setWordWrap(True)

        actions = QWidget(self)
        actions_row = QHBoxLayout(actions)
        actions_row.setContentsMargins(0, 0, 0, 0)
        actions_row.setSpacing(8)
        actions_row.addWidget(load, 1)
        actions_row.addWidget(search, 1)

        self.sync_strip = SyncStrip(self)
        self.sync_strip.seek_requested.connect(self.seek_requested)
        self._sync_timer = QTimer(self)
        self._sync_timer.setInterval(120)
        self._sync_timer.timeout.connect(self._tick_sync)

        return self._build_page(
            "Subtitles",
            "Pick a track here; V cycles them. Z / X nudge the timing.",
            [
                ("Tracks", [self.subtitle_tracks]),
                (
                    "Sync",
                    [
                        self._sync_hint(),
                        self.sync_strip,
                        self.subtitle_delay,
                        StepperRow(settings, "sub.step_ms", "Delay step", 10, 500, 10, suffix=" ms"),
                    ],
                ),
                (
                    "Appearance",
                    [
                        StepperRow(settings, "sub.text_scale", "Size", 50, 300, 10, suffix=" %"),
                        StepperRow(settings, "sub.margin_pct", "Height from bottom", 0, 45, 3, suffix=" %"),
                        StepperRow(settings, "sub.background", "Background", 0, 100, 10, suffix=" %"),
                        SegmentRow(
                            settings,
                            "sub.outline",
                            "Outline",
                            [
                                ("none", "None"),
                                ("thin", "Thin"),
                                ("normal", "Normal"),
                                ("thick", "Thick"),
                            ],
                        ),
                        self._style_note,
                    ],
                ),
                # One row of actions, then the online account settings under their
                # own heading: a lone primary button between two text fields read
                # as if it belonged to the field above it.
                ("Get subtitles", [actions]),
                (
                    "OpenSubtitles",
                    [
                        TextRow(
                            settings,
                            "sub.api_key",
                            "API key",
                            hint="Free from opensubtitles.com \u2192 Settings \u2192 API.",
                            placeholder="Paste your key",
                            width=168,
                        ),
                        TextRow(
                            settings,
                            "sub.languages",
                            "Languages",
                            hint="ISO codes, comma separated.",
                            placeholder="en, es, fr",
                            width=168,
                        ),
                    ],
                ),
            ],
        )

    # ----------------------------------------------------------------- search
    def _build_search_bar(self) -> QWidget:
        bar = QWidget(self._card)
        bar.setObjectName("SearchBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(14, 12, 14, 12)
        row.setSpacing(8)

        self._search_field = QLineEdit(bar)
        self._search_field.setObjectName("SearchField")
        self._search_field.setPlaceholderText("Search settings…")
        self._search_field.setClearButtonEnabled(True)
        self._search_field.textChanged.connect(lambda _text: self._run_search())
        self._search_field.installEventFilter(self)

        self._scope = QComboBox(bar)
        self._scope.setObjectName("SearchScope")
        self._scope.addItems(["This tab", "All tabs"])
        self._scope.currentIndexChanged.connect(lambda _index: self._run_search())
        row.addWidget(self._search_field, 1)
        row.addWidget(self._scope)
        return bar

    def _build_results_page(self) -> QWidget:
        page = QWidget(self._card)
        page.setObjectName("SearchResultsPage")
        self._result_rows = QVBoxLayout()
        self._result_rows.setContentsMargins(16, 10, 16, 14)
        self._result_rows.setSpacing(6)
        holder = QWidget(page)
        holder.setObjectName("RowsHolder")
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.addLayout(self._result_rows)
        holder_layout.addStretch(1)

        self._result_summary = QLabel("", page)
        self._result_summary.setObjectName("PageHint")

        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addSpacing(10)
        layout.addWidget(self._result_summary)
        layout.addWidget(self._scroll(holder), 1)
        return page

    def _index_entries(self) -> None:
        """Collect every searchable row once, after all pages exist."""
        if getattr(self, "_entries", None):
            return
        entries: list[_Entry] = []
        seen: set[tuple[str, str]] = set()

        def collect(page: QWidget, tab: str, subtab: int | None) -> None:
            for widget in page.findChildren(QWidget):
                title = getattr(widget, "search_title", None)
                if title is None:
                    continue
                text = title()
                key = (tab, text)
                if not text or key in seen:
                    continue
                seen.add(key)
                hint = getattr(widget, "search_hint", lambda: "")()
                section = widget.property("searchSection") or ""
                entries.append(_Entry(tab, subtab, text, f"{section} {hint}".strip(), widget))

        for index in range(min(self._stack.count(), len(TABS))):
            page = self._stack.widget(index)
            tab = TABS[index][0]
            if page is self._stack.widget(0):
                for sub in range(self._sub_stack.count()):
                    collect(self._sub_stack.widget(sub), tab, sub)
            else:
                collect(page, tab, None)
        self._entries = entries

    def open_search(self) -> None:
        """Ctrl+F: search inside settings, scoped to the tab you are on."""
        self._index_entries()
        current = self._stack.currentIndex()
        self._search_origin = self._last_tab_index if current >= len(TABS) else current
        # "This tab" means the page you are looking at, and the Settings page is
        # three sub-tabs wide - scoping to "Settings" there returned rows from
        # Keybindings, Playback and Interface at once, which reads as global.
        self._search_subtab = self._sub_stack.currentIndex() if self._search_origin == 0 else None
        self._scope.setCurrentIndex(0)  # every visit starts scoped, never global
        self._search_bar.show()
        # Unlike the panel itself (shown without activation so player shortcuts
        # keep working), Ctrl+F is an explicit "let me type" - take focus for it.
        self.activateWindow()
        self._search_field.setFocus()
        self._search_field.selectAll()
        self._run_search()

    def close_search(self) -> None:
        self._search_bar.hide()
        self._search_field.clear()
        self._show_tab(self._search_origin)

    def _run_search(self) -> None:
        query = self._search_field.text().strip().casefold()
        if not query:
            if self._stack.currentIndex() == self._results_index:
                self._show_tab(self._search_origin)
            return
        scope_all = self._scope.currentIndex() == 1
        origin = TABS[self._search_origin][0]
        subtab = self._search_subtab

        def in_scope(entry: "_Entry") -> bool:
            if scope_all:
                return True
            if entry.tab != origin:
                return False
            if subtab is None or entry.subtab is None:
                return True
            return entry.subtab == subtab

        hits = [
            entry
            for entry in self._entries
            if in_scope(entry)
            and (query in entry.title.casefold() or query in entry.hint.casefold())
        ]
        self._fill_results(hits, query, scope_all)
        self._show_tab(self._results_index)

    def _fill_results(self, hits: list["_Entry"], query: str, scope_all: bool) -> None:
        while self._result_rows.count():
            item = self._result_rows.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        if scope_all:
            where = "every tab"
        elif self._search_subtab is None:
            where = TABS[self._search_origin][1]
        else:
            where = _SUB_LABELS[self._search_subtab]
        if not hits:
            self._result_summary.setText(f"Nothing matching “{query}” in {where}.")
            return
        self._result_summary.setText(
            f"{len(hits)} result{'s' if len(hits) != 1 else ''} in {where}"
        )
        for entry in hits:
            button = QPushButton(entry.title, self._results_page)
            button.setObjectName("SearchResult")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            tip = " · ".join(part for part in (entry.hint, entry.path()) if part)
            button.setToolTip(tip)
            button.clicked.connect(lambda _checked=False, target=entry: self._goto_entry(target))
            self._result_rows.addWidget(button)

    def _goto_entry(self, entry: "_Entry") -> None:
        tab_index = [key for key, _label, _icon in TABS].index(entry.tab)
        self._search_bar.hide()
        self._search_field.clear()
        self._show_tab(tab_index)
        if entry.subtab is not None:
            self._show_sub_tab(entry.subtab)
        self._flash(entry.widget)

    def _flash(self, widget: QWidget) -> None:
        # A plain QWidget ignores a QSS background unless it is told to paint one;
        # without this the flash property was set and styled but never drawn.
        widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        widget.setProperty("flash", True)
        widget.style().unpolish(widget)
        widget.style().polish(widget)

        def clear() -> None:
            try:
                widget.setProperty("flash", False)
                widget.style().unpolish(widget)
                widget.style().polish(widget)
            except RuntimeError:  # the row may be gone (new file, rebuilt list)
                pass

        QTimer.singleShot(1200, clear)

    def _show_tab(self, index: int) -> None:
        if index < 0 or index >= self._stack.count() or index == self._stack.currentIndex():
            return
        if index < len(TABS):
            self._last_tab_index = index
        self._stack.setCurrentIndex(index)
        self._page_host.slide_in()

    def _show_sub_tab(self, index: int) -> None:
        if index < 0 or index >= self._sub_stack.count() or index == self._sub_stack.currentIndex():
            return
        self._sub_stack.setCurrentIndex(index)
        self._sub_host.slide_in()

    # -------------------------------------------------------------------- api
    def open_tab(self, name: str) -> None:
        if name not in self._tab_buttons:
            name = TAB_SETTINGS
        self._current = name
        self._tab_buttons[name].setChecked(True)
        self._show_tab([key for key, _label, _icon in TABS].index(name))
        self.refresh_dynamic()
        self.sync_geometry()
        opening = not self.isVisible() or self._closing_slide
        self._closing_slide = False
        self.show()
        self.raise_()
        self.activateWindow()
        if opening:
            self._slide_in()
        self.visibility_changed.emit(True)

    def toggle_tab(self, name: str) -> None:
        if self.isVisible() and self._current == name and not self._closing_slide:
            self.close_panel()
        else:
            self.open_tab(name)

    def close_panel(self) -> None:
        if self.isVisible() and not self._closing_slide:
            self._slide_out()

    def is_open(self) -> bool:
        return self.isVisible()

    def refresh_dynamic(self) -> None:
        self._refresh_rows()
        engine = self._engine
        if engine is None:
            return
        try:
            self.audio_tracks.set_tracks(engine.audio_tracks(), engine.current_audio())
            self.subtitle_tracks.set_tracks(
                [(-1, "Off"), *engine.subtitle_tracks()], engine.current_subtitle()
            )
        except Exception:  # noqa: BLE001 - a torn-down engine must not break the panel
            self.audio_tracks.set_tracks([], -1)
            self.subtitle_tracks.set_tracks([], -1)
        self.audio_delay.refresh()
        self.subtitle_delay.refresh()

    def refresh_timing(self) -> None:
        """Re-read the delays after a shortcut changed them behind our back."""
        if self._current == TAB_SUBTITLES:
            self.subtitle_delay.refresh()
        elif self._current == TAB_AUDIO:
            self.audio_delay.refresh()

    def refresh_tracks(self) -> None:
        """Called by the player after a file opens or a track changes."""
        if self.isVisible():
            self.refresh_dynamic()

    def set_map_note(self, text: str) -> None:
        """Describe the sound map currently drawn behind the seek bar."""
        if getattr(self, "map_note", None) is not None:
            self.map_note.setText(text)

    def set_engine(self, engine) -> None:  # noqa: ANN001
        self._engine = engine
        self.refresh_dynamic()

    # ---------------------------------------------------------------- slots
    def _on_audio_chosen(self, track_id: int) -> None:
        if self._engine is not None:
            self._engine.set_audio(track_id)
            self.refresh_dynamic()

    def _on_subtitle_chosen(self, track_id: int) -> None:
        if self._engine is not None:
            self._engine.set_subtitle(track_id)
            self.refresh_dynamic()

    def _on_changed(self, key: str) -> None:
        if key.startswith(shortcuts.KEYS_PREFIX):
            self._refresh_rows()

    def _refresh_rows(self) -> None:
        for row in self._rows:
            row.refresh()

    def _show_status(self, message: str) -> None:
        self._status.setText(message)
        self._status_timer.start()

    def _reset_all(self) -> None:
        self._bindings.reset()
        self._refresh_rows()
        self._show_status("All shortcuts reset to defaults")

    # -------------------------------------------------------------- geometry
    def _target_rect(self) -> QRect:
        client = self._host.rect()
        # Never wider than the window: on a small window the panel becomes a
        # sheet rather than hanging over the desktop edge.
        width = min(tokens.SIDEBAR_W, client.width())
        origin = self._host.mapToGlobal(client.topLeft())
        return QRect(origin.x() + client.width() - width, origin.y(), width, max(240, client.height()))

    def sync_geometry(self) -> None:
        """Flush right, full height - the mirror of the file drawer."""
        self._rest = self._target_rect()
        if self._slide.state() != QPropertyAnimation.State.Running:
            self.setGeometry(self._rest)

    def _flush_right(self) -> QPoint:
        return QPoint(self._rest.x() + _SLIDE_PX, self._rest.y())

    def _slide_in(self) -> None:
        # Starts partly opaque: fading in from 0.0 made the panel look empty for
        # the first frames, which is what read as "the items take a second".
        self._closing_slide = False
        self.move(self._flush_right())
        self._run(self._flush_right(), self._rest.topLeft(), 0.55, 1.0)

    def _slide_out(self) -> None:
        self._closing_slide = True
        self._run(self.pos(), self._flush_right(), self._effect.opacity(), 0.0)

    def _run(self, start: QPoint, end: QPoint, alpha_from: float, alpha_to: float) -> None:
        self._slide.stop()
        self._fade.stop()
        self._slide.setStartValue(start)
        self._slide.setEndValue(end)
        self._fade.setStartValue(alpha_from)
        self._fade.setEndValue(alpha_to)
        self._slide.start()
        self._fade.start()

    def _on_slide_finished(self) -> None:
        if self._closing_slide and self._effect.opacity() <= 0.01:
            self._closing_slide = False
            self.hide()
            self.visibility_changed.emit(False)

    # ---------------------------------------------------------------- events
    def set_sync_source(self, levels: list[int] | None, cues: list[tuple[int, int]] | None, duration: int) -> None:
        """Feed the sync strip: loudness map, caption cues, media duration."""
        self.sync_strip.set_levels(levels)
        self.sync_strip.set_cues(cues)
        self.sync_strip.set_duration(duration)
        self._tick_sync()

    def _tick_sync(self) -> None:
        if self._engine is None:
            return
        self.sync_strip.set_position(self._engine.position_ms())
        self.sync_strip.set_delay(self._engine.subtitle_delay())

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        # No refresh here: open_tab() already refreshed before showing, and doing
        # it twice doubled the cost of every open (28 keybinding rows rebuilt).
        self._sync_timer.start()
        self._tick_sync()

    def changeEvent(self, event: QEvent) -> None:
        """Close when the window as a whole loses activation.

        ActivationChange is the right event, not FocusOut: moving between the
        panel's own widgets leaves the window active, so only clicking back on the
        player (or another app) closes it. Modal dialogs opened from the panel -
        subtitle search - are exempt, or the panel would shut behind them.
        """
        if event.type() == QEvent.Type.ActivationChange and self.isVisible():
            active_modal = QApplication.activeModalWidget()
            if not self.isActiveWindow() and not self._closing_slide and active_modal is None:
                self.close_panel()
        super().changeEvent(event)

    def hideEvent(self, event: QHideEvent) -> None:
        super().hideEvent(event)
        self._sync_timer.stop()
        self.visibility_changed.emit(False)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close_panel()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            self.menu_requested.emit(event.globalPosition().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)


__all__ = ["SettingsPanel", "TAB_AUDIO", "TAB_SETTINGS", "TAB_SUBTITLES", "TABS"]
