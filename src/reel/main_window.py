"""The main window: owns the video surface, the engine and the control overlay.

Everything the user can do (keys, buttons, double-click, drops) funnels into the
`action_*` methods below, which is what the shortcut table points at. The engine
is the only thing that touches libVLC; this window only issues commands and
mirrors state back into the UI.
"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import (
    QCloseEvent,
    QCursor,
    QDragEnterEvent,
    QDropEvent,
    QShowEvent,
)
from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow, QWidget

from . import shortcuts
from .dropfiles import DropFileFilter, accept_files
from .engine import Player
from .feedback import Feedback
from .overlay import Overlay
from .style import tokens
from .video_surface import VideoSurface
from .widgets import ControlBar
from .win_dwm import apply_dark_titlebar

EMPTY_HINT = "Open a file   \u00b7   Ctrl+O   \u00b7   or drop it here"

MEDIA_FILTER = (
    "Video (*.mkv *.mp4 *.webm *.avi *.mov *.m4v *.ts *.m2ts *.mpg *.mpeg *.wmv *.flv *.ogv);;"
    "Audio (*.mp3 *.flac *.aac *.m4a *.opus *.ogg *.wav *.wma);;"
    "All files (*.*)"
)

DEFAULT_SIZE = (1280, 720)
MIN_SIZE = (480, 270)

SEEK_MS = 5_000
LONG_SEEK_MS = 30_000
SHORT_SEEK_MS = 10_000
VOLUME_STEP = 5


class MainWindow(QMainWindow):
    def __init__(self, media_path: str | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.setWindowTitle("Reel")
        self.resize(*DEFAULT_SIZE)
        self.setMinimumSize(*MIN_SIZE)
        self.setAcceptDrops(True)  # drops on the frame; the video area needs WM_DROPFILES

        self.surface = VideoSurface(self)
        self.surface.setObjectName("Surface")
        self.setCentralWidget(self.surface)

        # Empty state is painted by the surface itself: a Qt child widget here
        # would paint over libVLC's frames, which share the same HWND.
        self.surface.set_empty_text(EMPTY_HINT)

        self.engine = Player(self.surface.hwnd(), parent=self)
        self.engine.opened.connect(self._on_opened)
        self.engine.error.connect(self._on_error)
        self.engine.ended.connect(self._on_ended)
        self.engine.state_changed.connect(self._on_state_changed)

        # Controls cannot live in this window: libVLC's native child HWND is
        # composited above anything Qt paints here. Hence the separate window.
        self.overlay = Overlay(self)
        self.feedback = Feedback(self)
        self.controls = ControlBar(self.engine, self)
        self.overlay.set_content(self.controls)
        self.controls.set_loaded(False)

        self.surface.activity.connect(self.overlay.wake)
        self.surface.clicked.connect(self._on_surface_clicked)
        self.surface.double_clicked.connect(self._toggle_fullscreen)
        self.surface.wheel.connect(self._on_wheel)
        self.overlay.play_toggle_requested.connect(self.action_play_pause)
        self.overlay.visibility_changed.connect(self._on_overlay_visibility)
        self.controls.open_requested.connect(self._open_dialog)
        self.controls.fullscreen_requested.connect(self._toggle_fullscreen)
        self.controls.interaction.connect(self.overlay.hold)

        self._shortcuts = shortcuts.attach(self)  # keep the router alive
        # Cursors are created after QApplication exists - QCursor is a QGuiApplication
        # resource, so module-level construction breaks this module's import order.
        self._blank_cursor = QCursor(Qt.CursorShape.BlankCursor)
        self._arrow_cursor = QCursor(Qt.CursorShape.ArrowCursor)
        self._media_path = media_path
        self._themed = False
        self._at_end = False
        self._wheel_rest = 0
        self._closing = False
        self._drop_filter: DropFileFilter | None = None

    # ------------------------------------------------------------------ media
    def open_file(self, path: str) -> None:
        self._at_end = False
        self.surface.set_empty_text(EMPTY_HINT)
        if self.engine.open(path):
            self.setWindowTitle(f"{Path(path).name} \u2014 Reel")
            self.overlay.wake()

    def _on_opened(self, duration_ms: int, has_video: bool) -> None:  # noqa: ARG002
        self._at_end = False
        if has_video:
            self.surface.set_empty_text(None)
        self.overlay.wake()

    def _on_ended(self) -> None:
        # Stay on the last frame; the strip is how the user restarts.
        self._at_end = True
        self.overlay.hold(True)
        self.overlay.wake()

    def _on_state_changed(self, playing: bool) -> None:
        if self._closing:
            return
        # Paused means the strip stays put: no reason to hide the only way back.
        self.overlay.hold(not playing)
        self.overlay.wake()

    def _on_error(self, message: str) -> None:
        self._at_end = False
        self.surface.set_empty_text(message)

    # --------------------------------------------------------------- commands
    def action_play_pause(self) -> None:
        if self._at_end:
            self._at_end = False
            self.engine.replay()
            self.feedback.show_icon("play")
            return
        was_playing = self.engine.is_playing()
        self.engine.toggle()
        # The flash mirrors the key you just pressed, not the new bar state.
        self.feedback.show_icon("pause" if was_playing else "play")

    def action_seek_back_5(self) -> None:
        self._seek_by(-SEEK_MS)

    def action_seek_fwd_5(self) -> None:
        self._seek_by(SEEK_MS)

    def action_seek_back_30(self) -> None:
        self._seek_by(-LONG_SEEK_MS)

    def action_seek_fwd_30(self) -> None:
        self._seek_by(LONG_SEEK_MS)

    def action_seek_back_10(self) -> None:
        self._seek_by(-SHORT_SEEK_MS)

    def action_seek_fwd_10(self) -> None:
        self._seek_by(SHORT_SEEK_MS)

    def action_volume_up(self) -> None:
        self._step_volume(VOLUME_STEP)

    def action_volume_down(self) -> None:
        self._step_volume(-VOLUME_STEP)

    def action_mute(self) -> None:
        self.engine.set_muted(not self.engine.is_muted())
        self.feedback.show_icon("volume_mute" if self.engine.is_muted() else "volume_high")

    def action_fullscreen(self) -> None:
        if not self.isFullScreen():
            self._toggle_fullscreen()

    def action_exit_fullscreen(self) -> None:
        if self.isFullScreen():
            self._toggle_fullscreen()

    def action_open(self) -> None:
        self._open_dialog()

    def action_quit(self) -> None:
        self.close()

    # ----------------------------------------------------------------- helpers
    def _seek_by(self, delta_ms: int) -> None:
        if self.engine.duration_ms() <= 0:
            return
        self._at_end = False
        self.engine.seek_by(delta_ms)
        self.overlay.wake()

    def _step_volume(self, delta: int) -> None:
        self.engine.set_volume(max(0, min(100, self.engine.volume() + delta)))
        self._show_volume_feedback()

    def _show_volume_feedback(self) -> None:
        volume = self.engine.volume()
        muted = self.engine.is_muted()
        if muted or volume <= 0:
            icon = "volume_mute"
        elif volume < 50:
            icon = "volume_low"
        else:
            icon = "volume_high"
        self.feedback.show_level(icon, volume / 100.0, f"{volume}%")

    def _on_wheel(self, delta: int) -> None:
        """Wheel over the picture = volume. 120 units is one notch on Windows.

        Accumulated because touchpads send fractions of a notch.
        """
        self._wheel_rest += delta
        notches = int(self._wheel_rest / 120)
        if notches == 0:
            return
        self._wheel_rest -= notches * 120
        self._step_volume(notches * VOLUME_STEP)

    def _open_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open media", "", MEDIA_FILTER)
        if path:
            self.open_file(path)

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()
        self.controls.set_fullscreen(self.isFullScreen())
        self.overlay.sync_geometry()

    def _on_surface_clicked(self) -> None:
        self.action_play_pause()

    def _on_overlay_visibility(self, awake: bool) -> None:
        # Cursor hides with the controls, but only over live playback.
        hidden = not awake and self.engine.is_playing()
        self.surface.setCursor(self._blank_cursor if hidden else self._arrow_cursor)

    # ------------------------------------------------------------- drag & drop
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        self._open_dropped(paths)
        event.acceptProposedAction()

    def _open_dropped(self, paths: list[str]) -> None:
        for path in paths:
            if path and os.path.isfile(path):
                self.open_file(path)
                return

    # ------------------------------------------------------------------ events
    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not self._themed:
            self._themed = True
            apply_dark_titlebar(self, tokens.TITLEBAR, tokens.TEXT, tokens.TITLEBAR)
            self._install_drop_filter()
        if self._media_path:
            path, self._media_path = self._media_path, None
            QTimer.singleShot(0, lambda: self.open_file(path))

    def _install_drop_filter(self) -> None:
        """Shell drops over our own surface HWND (idle state, frame edges)."""
        accept_files(self.surface.hwnd(), True)
        self._drop_filter = DropFileFilter(self._open_dropped)
        app = QApplication.instance()
        if app is not None:
            app.installNativeEventFilter(self._drop_filter)

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.WindowStateChange:
            self.controls.set_fullscreen(self.isFullScreen())
        super().changeEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        # The strip and the flash are separate top-level windows. Closing only the
        # player leaves them on screen (and keeps the app alive, since a visible
        # window remains), which looked like a transparent window over the desktop.
        self._closing = True
        for window in (self.overlay, self.feedback):
            window.hide()
            window.close()
        if self._drop_filter is not None:
            app = QApplication.instance()
            if app is not None:
                app.removeNativeEventFilter(self._drop_filter)
            self._drop_filter = None
        self.hide()  # take the surface off screen before VLC tears its vout down
        self.engine.release()
        super().closeEvent(event)
