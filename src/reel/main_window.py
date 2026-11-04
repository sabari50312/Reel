"""The main window: owns the video surface, the engine and the control overlay.

Everything the user can do (keys, buttons, double-click, drops) funnels into the
`action_*` methods below, which is what the shortcut table points at. The engine
is the only thing that touches libVLC; this window only issues commands and
mirrors state back into the UI.
"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, QTimer
from PySide6.QtGui import (
    QActionGroup,
    QCloseEvent,
    QCursor,
    QDragEnterEvent,
    QDropEvent,
    QShowEvent,
)
from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow, QMenu, QMessageBox, QWidget

from . import shortcuts
from .audiomap import AudioMapService
from .dropfiles import DropFileFilter, accept_files
from .engine import Player
from .file_panel import FilePanel
from .feedback import Feedback
from .overlay import Overlay
from .resume_prompt import ResumePrompt
from .settings import ResumeStore, Settings
from .settings_panel import TAB_AUDIO, TAB_SETTINGS, TAB_SUBTITLES, SettingsPanel
from .subparse import parse_cues
from .subtitle_search import SubtitleSearch
from .style import tokens
from .video_surface import VideoSurface
from .widgets import ControlBar
from .win_dwm import apply_dark_titlebar, set_topmost

EMPTY_HINT = "Double-click a file to play   ·   Ctrl+O to browse   ·   or drop it here"

MEDIA_FILTER = (
    "Video (*.mkv *.mp4 *.webm *.avi *.mov *.m4v *.ts *.m2ts *.mpg *.mpeg *.wmv *.flv *.ogv);;"
    "Audio (*.mp3 *.flac *.aac *.m4a *.opus *.ogg *.wav *.wma);;"
    "All files (*.*)"
)

DEFAULT_SIZE = (1280, 720)
MIN_SIZE = (480, 270)

VOLUME_STEP = 5
WHEEL_NOTCH = 120  # Windows sends 120 units per wheel notch

SPEEDS: tuple[tuple[float, str], ...] = (
    (0.5, "0.5x"),
    (0.75, "0.75x"),
    (1.0, "Normal"),
    (1.25, "1.25x"),
    (1.5, "1.5x"),
    (2.0, "2x"),
)
ASPECTS: tuple[tuple[str, str], ...] = (
    ("", "Default"),
    ("16:9", "16:9"),
    ("16:10", "16:10"),
    ("4:3", "4:3"),
    ("5:4", "5:4"),
    ("1:1", "1:1"),
    ("1.85:1", "1.85:1"),
    ("2.21:1", "2.21:1"),
    ("2.35:1", "2.35:1"),
    ("2.39:1", "2.39:1"),
)
ZOOMS: tuple[tuple[float, str], ...] = (
    (0.0, "Fit to window"),
    (0.25, "1/4"),
    (0.5, "1/2"),
    (1.0, "1x"),
    (2.0, "2x"),
)
SPEED_STEPS: tuple[float, ...] = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
SUBTITLE_EXTS = frozenset({".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup"})


def sidecar_subtitle(path: str) -> str | None:
    """The subtitle file libVLC would auto-load for `path`, if there is one.

    Mirrors VLC's autodetect: same folder, a name that starts with the video's
    stem (so `film.en.srt`, `film.forced.ass` count), then a `Subs/` subfolder.
    The shortest name wins, which prefers `film.srt` over `film.en.srt`.
    """
    try:
        source = Path(path)
        stem = source.stem
        folder = source.parent
        found: list[Path] = []
        for entry in folder.iterdir():
            if entry.is_file() and entry.suffix.lower() in SUBTITLE_EXTS:
                if entry.stem == stem or entry.stem.startswith(f"{stem}."):
                    found.append(entry)
        if not found:
            subs = folder / "Subs"
            if subs.is_dir():
                for entry in subs.iterdir():
                    if entry.is_file() and entry.suffix.lower() in SUBTITLE_EXTS:
                        if entry.stem == stem or entry.stem.startswith(f"{stem}."):
                            found.append(entry)
        if not found:
            return None
        found.sort(key=lambda item: (len(item.name), item.name.lower()))
        return str(found[0])
    except OSError:
        return None


class MainWindow(QMainWindow):
    def __init__(
        self,
        media_path: str | None = None,
        parent: QWidget | None = None,
        settings: Settings | None = None,
    ) -> None:
        super().__init__(parent)

        # Injectable so tests (and later a --settings-dir flag) can point it at a
        # scratch store instead of the user's real preferences.
        self.settings = settings if settings is not None else Settings(parent=self)
        self.setWindowTitle("Reel")
        self.setMinimumSize(*MIN_SIZE)
        self._restore_geometry()
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
        self.engine.subtitle_attached.connect(self._on_subtitle_attached)
        self.engine.volume_changed.connect(lambda value: self.settings.set("volume", value))
        self.engine.muted_changed.connect(lambda muted: self.settings.set("muted", muted))
        self.settings.changed.connect(self._on_setting_changed)
        # Restore the saved audio state before any file is opened, so the first
        # frame already plays at the right level instead of jumping after a second.
        self.engine.set_volume(int(self.settings.get("volume")))
        self.engine.set_muted(bool(self.settings.get("muted")))

        # Controls cannot live in this window: libVLC's native child HWND is
        # composited above anything Qt paints here. Hence the separate window.
        self.overlay = Overlay(self)
        self.overlay.set_hide_delay(int(self.settings.get("ui.overlay_delay_ms")))
        self.feedback = Feedback(self)
        self.files = FilePanel(self)
        self.audio_map = AudioMapService(self)
        self.audio_map.ready.connect(self._on_audio_map)
        self.audio_map.progress.connect(self._on_audio_map)
        self.audio_map.analysing.connect(self._on_analysing)
        self.controls = ControlBar(self.engine, self)
        self.overlay.set_content(self.controls)
        self.controls.set_loaded(False)

        self.surface.activity.connect(self.overlay.wake)
        self.surface.clicked.connect(self._on_surface_clicked)
        self.surface.double_clicked.connect(self._on_surface_double_clicked)
        self.surface.wheel.connect(self._on_wheel)
        self.surface.menu_requested.connect(self._show_menu)
        self.overlay.play_toggle_requested.connect(self.action_play_pause)
        self.overlay.menu_requested.connect(self._show_menu)
        self.overlay.visibility_changed.connect(self._on_overlay_visibility)
        self.controls.speed_requested.connect(self._show_speed_menu)
        self.controls.audio_requested.connect(self._open_audio_tab)
        self.controls.subtitles_requested.connect(self._open_subtitles_tab)
        self.controls.menu_requested.connect(self._show_menu)
        self.controls.fullscreen_requested.connect(self._toggle_fullscreen)
        self.controls.settings_requested.connect(self.action_settings)
        self.controls.interaction.connect(self.overlay.hold)
        self.controls.files_requested.connect(self._toggle_files)
        self.files.file_chosen.connect(self._on_file_chosen)
        self.files.menu_requested.connect(self._show_menu)

        self._shortcuts = shortcuts.attach(self, self.settings)  # keep the router alive
        # Cursors are created after QApplication exists - QCursor is a QGuiApplication
        # resource, so module-level construction breaks this module's import order.
        self._blank_cursor = QCursor(Qt.CursorShape.BlankCursor)
        self._arrow_cursor = QCursor(Qt.CursorShape.ArrowCursor)
        self._resume = ResumeStore(self.settings)
        self._prompt = ResumePrompt(self)
        self._prompt.chosen.connect(self._on_resume_choice)
        self._pending_resume = 0
        self._resume_timer = QTimer(self)
        self._resume_timer.setInterval(10_000)
        self._resume_timer.timeout.connect(self._remember_position)
        self._media_path = media_path
        self._themed = False
        self._at_end = False
        self._wheel_rest = 0
        self._closing = False
        self._drop_filter: DropFileFilter | None = None
        self._files_was_open = False
        self.panel = SettingsPanel(self.settings, self, self.engine)
        self.panel.visibility_changed.connect(self._on_settings_visibility)
        self.panel.search_requested.connect(self.action_subtitle_search)
        self.panel.load_subtitle_requested.connect(self.action_open_subtitle)
        self.panel.menu_requested.connect(self._show_menu)
        self.panel.seek_requested.connect(self.engine.seek_ms)
        self._search_window: SubtitleSearch | None = None
        self._subtitle_fallback: tuple[str, str] | None = None
        self._audio_levels: list[int] | None = None
        self._sub_cues: list[tuple[int, int]] = []

    # ---------------------------------------------------------------- settings
    def _on_setting_changed(self, key: str) -> None:
        """Live-apply: a setting changed (from here or from the settings window).

        Engine setters re-emit volume_changed/muted_changed, which write the same
        value back into Settings - harmless, because Settings.set() ignores no-ops
        and so never re-emits.
        """
        if key == "volume":
            self.engine.set_volume(int(self.settings.get("volume")))
        elif key == "muted":
            self.engine.set_muted(bool(self.settings.get("muted")))
        elif key == "ui.overlay_delay_ms":
            self.overlay.set_hide_delay(int(self.settings.get("ui.overlay_delay_ms")))
        elif key == "ui.always_on_top":
            self._apply_topmost()
        elif key == "input.wheel":
            self._wheel_rest = 0  # do not carry half a notch into the new mode

    def _step_ms(self) -> int:
        """Small seek step (←/→), read on every press so edits apply without restart."""
        return int(self.settings.get("play.step_s")) * 1000

    def _short_step_ms(self) -> int:
        return int(self.settings.get("play.short_step_s")) * 1000

    def _long_step_ms(self) -> int:
        """Big seek step (Shift+←/→)."""
        return int(self.settings.get("play.long_step_s")) * 1000

    def _restore_geometry(self) -> None:
        if not self.settings.has("window_w"):  # first run: centre, do not trust 0,0
            self.resize(*DEFAULT_SIZE)
            screen = QApplication.primaryScreen()
            if screen is not None:
                self.move(screen.availableGeometry().center() - self.rect().center())
            return

        rect = QRect(
            int(self.settings.get("window_x")),
            int(self.settings.get("window_y")),
            int(self.settings.get("window_w")),
            int(self.settings.get("window_h")),
        )
        if any(screen.availableGeometry().intersects(rect) for screen in QApplication.screens()):
            self.setGeometry(rect)
        else:
            # Monitor removed since last run: keep the size, drop the position.
            self.resize(max(MIN_SIZE[0], rect.width()), max(MIN_SIZE[1], rect.height()))
        if bool(self.settings.get("window_maximized")):
            self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)

    def _save_geometry(self) -> None:
        # normalGeometry() is the pre-maximize/pre-fullscreen rect, so closing while
        # fullscreen does not store a fullscreen-sized window for the next launch.
        rect = self.normalGeometry() if self.isFullScreen() or self.isMaximized() else self.geometry()
        self.settings.set("window_x", rect.x())
        self.settings.set("window_y", rect.y())
        self.settings.set("window_w", rect.width())
        self.settings.set("window_h", rect.height())
        self.settings.set("window_maximized", self.isMaximized())

    # -------------------------------------------------------------------- media
    def open_file(self, path: str) -> None:
        self._remember_position()  # the outgoing file keeps its place
        self.files.close_panel()
        # A new film has neither its sound map nor its captions yet.
        self._audio_levels = None
        self._sub_cues = []
        self.panel.set_sync_source(None, [], 0)
        self._at_end = False
        self.surface.set_empty_text(EMPTY_HINT)
        mode = str(self.settings.get("files.resume"))
        start = self._resume.position(path) if mode != "off" else 0
        asking = mode == "ask" and start > 0
        # When asking, the file opens paused at 0 so the question is shown over a
        # still first frame; the position is applied once the user answers.
        self._pending_resume = start if asking else 0
        if self._prompt.isVisible():
            self._prompt.hide()
        if self.engine.open(
            path,
            autoplay=bool(self.settings.get("play.autoplay")) and not asking,
            start_ms=0 if asking else start,
        ):
            self.setWindowTitle(f"{Path(path).name} \u2014 Reel")
            self.settings.set("files.last_dir", str(Path(path).resolve().parent))
            self._resume_timer.start()
            self.overlay.wake()
            self._request_audio_map(path)
            # libVLC auto-loads a same-name subtitle by itself; it cannot tell us
            # the cue times, so find that file and parse it for the sync strip.
            sidecar = sidecar_subtitle(path)
            if sidecar:
                self._load_cues(sidecar)

    def _request_audio_map(self, path: str) -> None:
        """Paint loudness bands immediately when cached, otherwise async.

        With several audio tracks the map has to follow the active one, which means
        decoding that track (rate 1x, in the background); single-track files take
        the fast whole-file transcode instead.
        """
        self.controls.set_audio_map(None)
        track = None
        if len(self.engine.audio_tracks()) > 1:
            track = self.engine.current_audio()
            if track < 0:
                track = None
        self.controls.set_analysis(0.0)  # cleared below when the map is cached
        levels = self.audio_map.request(path, track)
        if levels:
            self.controls.set_analysis(None)
            self.controls.set_audio_map(levels)
            self._audio_levels = levels
            self.panel.sync_strip.set_levels(levels)

    def _on_analysing(self, path: str, fraction: float) -> None:
        # A negative fraction means "gave up" (no audio track, bad file, cancel).
        if path == self.engine.current_path():
            self.controls.set_analysis(None if fraction < 0 else fraction)

    def _on_audio_map(self, path: str, levels: object) -> None:
        if path == self.engine.current_path() and isinstance(levels, list):
            self.controls.set_audio_map(levels)
            self._audio_levels = levels
            self.panel.sync_strip.set_levels(levels)

    def _remember_position(self) -> None:
        """Store "where was I" for whatever is loaded right now."""
        if not self.settings.get("files.resume"):
            return
        path = self.engine.current_path()
        duration = self.engine.duration_ms()
        if not path or duration <= 0:
            return
        self._resume.remember(path, self.engine.position_ms(), duration)

    def _on_opened(self, duration_ms: int, has_video: bool) -> None:  # noqa: ARG002
        self._at_end = False
        self.controls.set_loaded(True)
        self.panel.refresh_tracks()
        self.panel.set_sync_source(self._audio_levels, self._sub_cues, duration_ms)
        if has_video:
            self.surface.set_empty_text(None)
        self.overlay.wake()
        if self._pending_resume and not self._prompt.isVisible():
            self._prompt.ask(self._pending_resume)

    def _on_resume_choice(self, cont: bool) -> None:
        position, self._pending_resume = self._pending_resume, 0
        if cont and position:
            self.engine.seek_ms(position)
        self.engine.play()

    def _on_ended(self) -> None:
        # Stay on the last frame; the strip is how the user restarts.
        finished = self.engine.current_path()
        if finished:
            self._resume.forget(finished)  # a finished film starts over next time
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
        if not self.engine.has_media():
            return  # empty window: no state to flip, no flash to show
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
        self._seek_by(-self._step_ms())

    def action_seek_fwd_5(self) -> None:
        self._seek_by(self._step_ms())

    def action_seek_back_30(self) -> None:
        self._seek_by(-self._long_step_ms())

    def action_seek_fwd_30(self) -> None:
        self._seek_by(self._long_step_ms())

    def action_seek_back_10(self) -> None:
        self._seek_by(-self._short_step_ms())

    def action_seek_fwd_10(self) -> None:
        self._seek_by(self._short_step_ms())

    def _refresh_audio_map(self) -> None:
        path = self.engine.current_path()
        if path:
            self._request_audio_map(path)

    def _choose_audio(self, track_id: int) -> None:
        self.engine.set_audio(track_id)
        self._refresh_audio_map()

    def action_audio_next(self) -> None:
        if not self.engine.has_media():
            return
        picked = self.engine.cycle_audio()
        label = next((name for track_id, name in self.engine.audio_tracks() if track_id == picked), "")
        self.overlay.wake()
        self.feedback.show_text(f"Audio: {label}" if label else "No audio track")
        self._refresh_audio_map()  # the bands follow the track you picked

    def action_subtitle_next(self) -> None:
        if not self.engine.has_media():
            return
        picked = self.engine.cycle_subtitle()
        label = next((name for track_id, name in self.engine.subtitle_tracks() if track_id == picked), "")
        self.overlay.wake()
        if picked < 0:
            self.feedback.show_text("Subtitles: off")
        else:
            self.feedback.show_text(f"Subtitles: {label}" if label else "Subtitles on")

    def action_audio_later(self) -> None:
        self._nudge_audio_delay(1)

    def action_audio_earlier(self) -> None:
        self._nudge_audio_delay(-1)

    def action_audio_delay_reset(self) -> None:
        self._set_audio_delay(0)

    def action_subtitle_later(self) -> None:
        self._nudge_subtitle_delay(1)

    def action_subtitle_earlier(self) -> None:
        self._nudge_subtitle_delay(-1)

    def action_subtitle_delay_reset(self) -> None:
        self._set_subtitle_delay(0)

    def action_speed_up(self) -> None:
        self._nudge_rate(1)

    def action_speed_down(self) -> None:
        self._nudge_rate(-1)

    def action_speed_normal(self) -> None:
        self._set_rate_value(1.0)

    def action_volume_up(self) -> None:
        self._step_volume(VOLUME_STEP)

    def action_volume_down(self) -> None:
        self._step_volume(-VOLUME_STEP)

    def action_mute(self) -> None:
        self.engine.set_muted(not self.engine.is_muted())
        self.feedback.show_icon("volume_mute" if self.engine.is_muted() else "volume_high")

    def action_fullscreen(self) -> None:
        # F toggles: the same key in and out, like every other player.
        self._toggle_fullscreen()

    def action_exit_fullscreen(self) -> None:
        # Esc means "back out of whatever is open", in the order a user expects:
        # the panel is a surface over the video, fullscreen is the window.
        if self.panel.is_open():
            self.panel.close_panel()
            return
        if self.isFullScreen():
            self._toggle_fullscreen()

    def action_open(self) -> None:
        """Ctrl+O opens our own browser, not a second OS window."""
        self._toggle_files()

    def action_subtitle_search(self) -> None:
        self._show_subtitle_search()

    def action_open_subtitle(self) -> None:
        if self.engine.duration_ms() <= 0:
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open subtitle",
            "",
            "Subtitles (*.srt *.ass *.ssa *.vtt *.sub *.idx *.sup);;All files (*.*)",
        )
        if path and not self._attach_subtitle_file(path):
            QMessageBox.warning(self, "Reel", "That subtitle file could not be loaded.")

    def action_settings(self) -> None:
        self._open_settings()

    def action_search_settings(self) -> None:
        """Ctrl+F opens the panel if needed, then its search field."""
        if not self.panel.is_open():
            self.panel.open_tab("settings")
        self.panel.open_search()

    def action_quit(self) -> None:
        self.close()

    # ---------------------------------------------------------------- settings
    def _open_settings(self) -> None:
        """Open the right sidebar on the Settings tab.

        Non-modal and live-applying: the player keeps playing behind it, and the
        key router ignores anything typed here (it filters by window), so editing
        a binding cannot fire a player action.
        """
        self.panel.toggle_tab(TAB_SETTINGS)

    def _open_audio_tab(self) -> None:
        self.panel.toggle_tab(TAB_AUDIO)

    def _open_subtitles_tab(self) -> None:
        self.panel.toggle_tab(TAB_SUBTITLES)

    def _show_subtitle_search(self) -> None:
        """Online subtitle search for whatever is playing."""
        path = self.engine.current_path()
        if not path or self.engine.duration_ms() <= 0:
            self.feedback.show_text("Open a video first")
            return
        if self._search_window is None:
            self._search_window = SubtitleSearch(self.settings, self)
            self._search_window.subtitle_ready.connect(self._on_subtitle_downloaded)
            self._search_window.visibility_changed.connect(self._on_settings_visibility)
        self._search_window.open_for(path)
        self._search_window.show()
        self._search_window.raise_()
        self._search_window.activateWindow()

    def _on_subtitle_downloaded(self, path: str) -> None:
        if self._attach_subtitle_file(path):
            self.feedback.show_text("Subtitles loaded")

    def _on_settings_visibility(self, visible: bool) -> None:
        """Park the player chrome while the settings dialog is up.

        The strip and the flash are separate topmost windows, so without this they
        float over the dialog: its buttons and the volume pill visibly hang above
        it.
        """
        if self._closing:
            return
        self.overlay.set_suppressed(visible)
        self.feedback.set_suppressed(visible)
        # The browser is a top-level too: park it (and bring it back afterwards).
        if visible:
            self._files_was_open = self.files.isVisible()
            self.files.close_panel()
        elif self._files_was_open:
            self._files_was_open = False
            self.files.open_at(self.files.current_directory())
        self._on_overlay_visibility(self.overlay.is_awake())

    # ----------------------------------------------------------------- helpers
    def _seek_by(self, delta_ms: int) -> None:
        if self.engine.duration_ms() <= 0:
            return
        self._at_end = False
        self.engine.seek_by(delta_ms)
        self.overlay.wake()

    def _step_ms(self) -> int:
        return int(self.settings.get("play.step_s")) * 1000

    def _long_step_ms(self) -> int:
        return int(self.settings.get("play.long_step_s")) * 1000

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
        """Wheel over the picture: volume, seek, or nothing - see input.wheel.

        120 units is one notch on Windows; accumulated because touchpads send
        fractions of a notch. Ignored entirely while the settings window is up,
        so a stray scroll cannot change the volume behind the dialog.
        """
        if self.panel.is_open():
            return
        if self._search_window is not None and self._search_window.isVisible():
            return
        mode = str(self.settings.get("input.wheel"))
        if mode == "ignore":
            return

        self._wheel_rest += delta
        notches = int(self._wheel_rest / WHEEL_NOTCH)
        if notches == 0:
            return
        self._wheel_rest -= notches * WHEEL_NOTCH

        if mode == "seek":
            self._seek_by(notches * self._step_ms())
        else:
            self._step_volume(notches * VOLUME_STEP)

    def _apply_topmost(self) -> bool:
        """SetWindowPos, not setWindowFlag: re-creating the window would wreck vout."""
        if not self.isVisible():
            return False  # applied from showEvent instead
        return set_topmost(self, bool(self.settings.get("ui.always_on_top")))

    def _set_rate(self, rate: float) -> None:
        self.engine.set_rate(rate)
        self.overlay.wake()

    def _set_aspect(self, ratio: str) -> None:
        self.engine.set_aspect(ratio)
        self.overlay.wake()

    def _set_crop(self, ratio: str) -> None:
        self.engine.set_crop(ratio)
        self.overlay.wake()

    def _set_zoom(self, factor: float) -> None:
        self.engine.set_scale(factor)
        self.overlay.wake()

    def _nudge_rate(self, direction: int) -> None:
        """Step the playback speed along the same ladder the menu lists."""
        current = self.engine.rate()
        index = min(
            range(len(SPEED_STEPS)), key=lambda i: abs(SPEED_STEPS[i] - current)
        )
        target = max(0, min(len(SPEED_STEPS) - 1, index + direction))
        self._set_rate_value(SPEED_STEPS[target])

    def _nudge_audio_delay(self, direction: int) -> None:
        step = int(self.settings.get("audio.step_ms"))
        self._set_audio_delay(self.engine.audio_delay() + direction * step)

    def _set_audio_delay(self, milliseconds: int) -> None:
        if not self.engine.has_media():
            return
        milliseconds = max(-600_000, min(600_000, int(milliseconds)))
        self.engine.set_audio_delay(milliseconds)
        self.panel.refresh_timing()
        self.overlay.wake()
        self.feedback.show_text(f"Audio delay {milliseconds:+d} ms")

    def _nudge_subtitle_delay(self, direction: int) -> None:
        step = int(self.settings.get("sub.step_ms"))
        self._set_subtitle_delay(self.engine.subtitle_delay() + direction * step)

    def _set_subtitle_delay(self, milliseconds: int) -> None:
        if self.engine.duration_ms() <= 0:
            return
        # Positive delays the subtitles (libVLC's own sign), so show that number.
        milliseconds = max(-600_000, min(600_000, int(milliseconds)))
        self.engine.set_subtitle_delay(milliseconds)
        self.panel.refresh_timing()
        self.overlay.wake()
        self.feedback.show_text(f"Subtitle delay {milliseconds:+d} ms")

    def _timing_menu(self, parent: QMenu, title: str, read, write, step: int) -> QMenu:
        """A "Delay" submenu: current value, one step either way, half a second, reset.

        Shared by audio and subtitles: they differ only in which libVLC clock
        they read and write, so the menu should not either.
        """
        menu = self._style_menu(parent.addMenu(title))
        now = read()
        header = menu.addAction(f"Now: {now:+d} ms")
        header.setEnabled(False)
        for delta in (-step, step, -500, 500):
            menu.addAction(f"{delta:+d} ms").triggered.connect(
                lambda _checked=False, d=delta: write(read() + d)
            )
        reset = menu.addAction("Reset to 0")
        reset.setEnabled(now != 0)
        reset.triggered.connect(lambda: write(0))
        return menu

    def _set_rate_value(self, rate: float) -> None:
        self.engine.set_rate(rate)
        self.overlay.wake()
        self.feedback.show_text(f"{rate:g}×")

    def _style_menu(self, menu: QMenu) -> QMenu:
        """Turn a QMenu into a rounded dark panel.

        A rounded popup needs a translucent window - QSS border-radius alone is
        clipped away on an opaque top-level - so this sets the popup flags and the
        attribute once, before the menu is first shown.
        """
        menu.setWindowFlags(
            Qt.WindowType.Popup
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        return menu

    def _popup_at(self, menu: QMenu, anchor: QWidget) -> None:
        """Show `menu` above `anchor` (the bar lives at the bottom of the screen)."""
        menu.adjustSize()
        pos = anchor.mapToGlobal(QPoint(0, 0))
        menu.exec(QPoint(pos.x(), pos.y() - menu.sizeHint().height() - 8))

    def _show_speed_menu(self) -> None:
        if self._closing or self.engine.duration_ms() <= 0:
            return
        self.overlay.wake()
        menu = self._style_menu(QMenu(self))
        menu.setObjectName("PlayerMenu")
        group = QActionGroup(menu)
        group.setExclusive(True)
        current = self.engine.rate()
        for rate, label in SPEEDS:
            entry = menu.addAction(label)
            entry.setCheckable(True)
            entry.setChecked(abs(current - rate) < 0.01)
            group.addAction(entry)
            entry.triggered.connect(lambda _checked=False, value=rate: self._set_rate(value))
        self._popup_at(menu, self.controls.speed_button())

    def _show_menu(self, global_pos: QPoint) -> None:
        if self._closing:
            return
        self.overlay.wake()
        self._style_menu(self._build_menu()).exec(global_pos)

    def _build_menu(self) -> QMenu:
        """Rebuilt on every open: streams can appear well after the file starts."""
        loaded = self.engine.duration_ms() > 0
        menu = self._style_menu(QMenu(self))
        menu.setObjectName("PlayerMenu")

        menu.addAction(
            "Pause" if self.engine.is_playing() else "Play", self.action_play_pause
        )
        menu.addSeparator()

        speed_menu = self._style_menu(menu.addMenu("Speed"))
        speed_group = QActionGroup(speed_menu)
        speed_group.setExclusive(True)
        current_rate = self.engine.rate()
        for rate, label in SPEEDS:
            entry = speed_menu.addAction(label)
            entry.setCheckable(True)
            entry.setChecked(abs(current_rate - rate) < 0.01)
            speed_group.addAction(entry)
            entry.triggered.connect(lambda _checked=False, value=rate: self._set_rate(value))
        speed_menu.setEnabled(loaded)

        aspect_menu = self._style_menu(menu.addMenu("Aspect ratio"))
        aspect_group = QActionGroup(aspect_menu)
        aspect_group.setExclusive(True)
        current_aspect = self.engine.aspect()
        for ratio, label in ASPECTS:
            entry = aspect_menu.addAction(label)
            entry.setCheckable(True)
            entry.setChecked(ratio == current_aspect)
            aspect_group.addAction(entry)
            entry.triggered.connect(lambda _checked=False, value=ratio: self._set_aspect(value))
        aspect_menu.setEnabled(loaded and self.engine.has_video())

        crop_menu = self._style_menu(menu.addMenu("Crop"))
        crop_group = QActionGroup(crop_menu)
        crop_group.setExclusive(True)
        current_crop = self.engine.crop()
        for ratio, label in ASPECTS:
            entry = crop_menu.addAction(label)
            entry.setCheckable(True)
            entry.setChecked(ratio == current_crop)
            crop_group.addAction(entry)
            entry.triggered.connect(lambda _checked=False, value=ratio: self._set_crop(value))
        crop_menu.setEnabled(loaded and self.engine.has_video())

        zoom_menu = self._style_menu(menu.addMenu("Zoom"))
        zoom_group = QActionGroup(zoom_menu)
        zoom_group.setExclusive(True)
        current_zoom = self.engine.scale()
        for factor, label in ZOOMS:
            entry = zoom_menu.addAction(label)
            entry.setCheckable(True)
            entry.setChecked(abs(factor - current_zoom) < 0.01)
            zoom_group.addAction(entry)
            entry.triggered.connect(lambda _checked=False, value=factor: self._set_zoom(value))
        zoom_menu.setEnabled(loaded and self.engine.has_video())

        menu.addSeparator()

        audio_menu = self._style_menu(menu.addMenu("Audio track"))
        audio_tracks = self.engine.audio_tracks()
        if audio_tracks:
            self._track_entries(
                audio_menu, audio_tracks, self.engine.current_audio(), self._choose_audio
            )
        else:
            audio_menu.addAction("No audio").setEnabled(False)
            audio_menu.setEnabled(loaded)

        subtitle_menu = self._style_menu(menu.addMenu("Subtitles"))
        subtitle_group = QActionGroup(subtitle_menu)
        subtitle_group.setExclusive(True)
        off = subtitle_menu.addAction("Off")
        off.setCheckable(True)
        off.setChecked(self.engine.current_subtitle() < 0)
        subtitle_group.addAction(off)
        off.triggered.connect(lambda _checked=False: self.engine.set_subtitle(-1))
        subtitle_tracks = self.engine.subtitle_tracks()
        if subtitle_tracks:
            subtitle_menu.addSeparator()
            self._track_entries(
                subtitle_menu,
                subtitle_tracks,
                self.engine.current_subtitle(),
                self.engine.set_subtitle,
                subtitle_group,
            )
        else:
            subtitle_menu.addAction("None in this file").setEnabled(False)
        subtitle_menu.addSeparator()
        subtitle_menu.addAction("Open subtitle file…", self.action_open_subtitle)
        subtitle_menu.addAction("Search online…  (Ctrl+Shift+S)", self.action_subtitle_search)

        self._timing_menu(
            subtitle_menu,
            "Delay",
            self.engine.subtitle_delay,
            self._set_subtitle_delay,
            int(self.settings.get("sub.step_ms")),
        ).setEnabled(loaded)

        audio_menu.addSeparator()
        audio_menu.addAction("Next track (B)", self.action_audio_next).setEnabled(
            len(audio_tracks) > 1
        )
        self._timing_menu(
            audio_menu,
            "Delay",
            self.engine.audio_delay,
            self._set_audio_delay,
            int(self.settings.get("audio.step_ms")),
        )
        subtitle_menu.addAction("Next track (V)", self.action_subtitle_next)
        subtitle_menu.setEnabled(loaded)

        menu.addSeparator()
        menu.addAction("Browse files…    Ctrl+O", self.action_open)
        menu.addAction("Settings…", self.action_settings)
        menu.addAction(
            "Exit fullscreen" if self.isFullScreen() else "Fullscreen",
            self._toggle_fullscreen,
        )
        return menu

    def _track_entries(
        self,
        submenu: QMenu,
        tracks: list[tuple[int, str]],
        current: int,
        apply,
        group: QActionGroup | None = None,
    ) -> None:
        """Checkable entries for one kind of stream; long names are elided."""
        exclusive = group if group is not None else QActionGroup(submenu)
        exclusive.setExclusive(True)
        for track_id, name in tracks:
            entry = submenu.addAction(name if len(name) <= 56 else name[:55] + "…")
            entry.setCheckable(True)
            entry.setChecked(track_id == current)
            exclusive.addAction(entry)
            entry.triggered.connect(lambda _checked=False, value=track_id: apply(value))

    # ---------------------------------------------------------------- browser
    def _toggle_files(self) -> None:
        hint = self.engine.current_path() or str(self.settings.get("files.last_dir") or "")
        self.files.toggle(hint or None)
        if self.files.isVisible():
            self.overlay.wake()

    def _on_file_chosen(self, path: str) -> None:
        self.files.close_panel()
        self.open_file(path)

    def _open_dialog(self) -> None:
        start_dir = str(self.settings.get("files.last_dir") or "")
        if start_dir and not Path(start_dir).is_dir():
            start_dir = ""  # renamed folder or a disconnected drive
        path, _ = QFileDialog.getOpenFileName(self, "Open media", start_dir, MEDIA_FILTER)
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
        if str(self.settings.get("input.click")) == "none":
            self.overlay.wake()  # a click still wakes the controls, it just does nothing
            return
        self.action_play_pause()

    def _on_surface_double_clicked(self) -> None:
        if str(self.settings.get("input.double_click")) == "none":
            return
        self._toggle_fullscreen()

    def _chrome_dialogs_open(self) -> bool:
        return (
            self.panel.is_open()
            or self.files.is_open()
            or (self._search_window is not None and self._search_window.isVisible())
        )

    def _on_overlay_visibility(self, awake: bool) -> None:
        # Cursor hides with the controls, but only over live playback - and never
        # while a panel is open, otherwise the pointer vanishes the moment it
        # leaves the panel and there is no way to bring it back.
        hidden = not awake and self.engine.is_playing() and not self._chrome_dialogs_open()
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
            if not path or not os.path.isfile(path):
                continue
            suffix = Path(path).suffix.lower()
            if suffix in SUBTITLE_EXTS and self._attach_subtitle_file(path):
                return
            self.open_file(path)
            return

    def _attach_subtitle_file(self, path: str) -> bool:
        """Show a dropped/selected subtitle file over the current media.

        Two paths, because `add_slave()` only works once the input is running and
        a refused attach is silent: try it live, and if no new track appears,
        re-open the same file with `:sub-file` and jump back to the old position.
        """
        source = self.engine.current_path()
        if not source or self.engine.duration_ms() <= 0:
            return False  # nothing to attach to: the caller opens it as media

        self.overlay.wake()
        self._load_cues(path)
        self._subtitle_fallback = (source, path)
        if not self.engine.attach_subtitle(path):
            # libVLC refused outright; no point waiting for the retry loop.
            self._subtitle_fallback = None
            self._reopen_with_subtitle(source, path)
        return True

    def _load_cues(self, path: str) -> None:
        """Cue timings for the sync strip come from the file, not from libVLC."""
        self._sub_cues = parse_cues(path)
        self.panel.sync_strip.set_cues(self._sub_cues)

    def _on_subtitle_attached(self, attached: bool) -> None:
        fallback, self._subtitle_fallback = self._subtitle_fallback, None
        if attached:
            self.feedback.show_text("Subtitles loaded")
            return
        if fallback is not None:
            self._reopen_with_subtitle(*fallback)
        else:
            self.feedback.show_text("Could not load subtitles")

    def _reopen_with_subtitle(self, source: str, path: str) -> None:
        """Last resort: re-open the same file with the subtitle baked in.

        Costs a brief reload, but it is the one path that always works, and a
        dropped subtitle that does nothing is worse than a half-second hiccup.
        """
        position = self.engine.position_ms()
        was_playing = self.engine.is_playing()
        self._load_cues(path)
        if self.engine.open(source, autoplay=was_playing, subtitle=path):
            QTimer.singleShot(700, lambda: self.engine.seek_ms(position))
            self.feedback.show_text("Subtitles loaded")
        else:
            self.feedback.show_text("Could not load subtitles")

    # ------------------------------------------------------------------ events
    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not self._themed:
            self._themed = True
            apply_dark_titlebar(self, tokens.TITLEBAR, tokens.TEXT, tokens.TITLEBAR)
            self._install_drop_filter()
            self._apply_topmost()
            if not self._media_path:
                # Nothing loaded: land on the browser instead of an empty picture.
                self.files.open_at(str(self.settings.get("files.last_dir") or "") or None)
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
        self._save_geometry()
        self._remember_position()
        if self._prompt.isVisible():
            self._prompt.hide()
        windows = [self.overlay, self.feedback, self.files, self.panel]
        if self._search_window is not None:
            windows.append(self._search_window)
        for window in windows:
            window.hide()
            window.close()
        if self._drop_filter is not None:
            app = QApplication.instance()
            if app is not None:
                app.removeNativeEventFilter(self._drop_filter)
            self._drop_filter = None
        self.audio_map.cancel()
        self.hide()  # take the surface off screen before VLC tears its vout down
        self.engine.release()
        self.settings.flush()
        super().closeEvent(event)
