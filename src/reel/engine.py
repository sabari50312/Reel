"""libVLC playback engine — the only module in Reel that imports `vlc`.

Design rules (see context.md):
  * UI never imports vlc; everything leaves this file as Qt signals.
  * Time is polled at ~30 Hz from a QTimer instead of relying on libVLC's
    coarse MediaPlayerTimeChanged events, which are far too jumpy for a
    smoothly filling seek bar.
  * libVLC callbacks arrive on VLC's own threads. They only ever emit Qt
    signals (thread-safe, auto-queued); they never touch widgets.
"""

from __future__ import annotations

import time
from pathlib import Path

import vlc
from PySide6.QtCore import QObject, Qt, QTimer, Signal

# Minimal flags: no VLC UI, no OSD, no title overlay, dumb interface.
VLC_ARGS = (
    "--intf=dummy",
    "--no-video-title-show",
    "--no-osd",
    "--quiet",
    "--file-caching=1000",
    "--network-caching=1500",
)

POLL_MS = 33  # ~30 Hz
_SUB_POLL_MS = 120  # attach_subtitle(): how often to look for the new track
_SUB_POLL_TRIES = 5
SEEK_GUARD_S = 0.35  # ignore polled positions this long after a seek


class Player(QObject):
    """Thin, Qt-flavoured facade over libVLC's MediaPlayer."""

    opened = Signal(int, bool)  # duration_ms, has_video
    state_changed = Signal(bool)  # is_playing
    position_changed = Signal(int)  # ms
    volume_changed = Signal(int)  # 0..100
    muted_changed = Signal(bool)
    ended = Signal()
    error = Signal(str)
    subtitle_attached = Signal(bool)  # attach_subtitle() finished (or gave up)

    # Internal hops from libVLC's worker thread to the GUI thread. Qt delivers
    # these queued because the emitting thread is not the receiver's thread, so
    # the slots run where QTimer/QWidget work is legal.
    _sig_state = Signal(bool)
    _sig_length = Signal(int)
    _sig_volume = Signal(int)
    _sig_end = Signal()
    _sig_error = Signal()
    _sig_vout = Signal()

    def __init__(self, hwnd: int = 0, parent: QObject | None = None) -> None:
        super().__init__(parent)

        self._instance = vlc.Instance(*VLC_ARGS)
        if self._instance is None:
            raise RuntimeError("libVLC failed to initialise (is VLC installed?)")

        self._player: vlc.MediaPlayer = self._instance.media_player_new()
        self._media: vlc.Media | None = None
        self._path: str | None = None  # kept for replay(): see replay()
        self._hwnd = hwnd
        self._duration = 0
        self._volume = 100  # cached: _player is gone after release()
        self._muted = False
        self._released = False
        self._seek_guard_until = 0.0
        self._video_reported = False  # see _on_vout()
        self._sub_pending: tuple[frozenset[int], int, str] | None = None  # see attach_subtitle()
        self._callbacks: list = []  # libVLC keeps C-level refs; we keep Python refs

        if hwnd:
            self.set_hwnd(hwnd)

        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.setTimerType(Qt.TimerType.CoarseTimer)
        self._poll.timeout.connect(self._on_poll)

        # QueuedConnection is explicit: the emitters are VLC-thread callbacks.
        queued = Qt.ConnectionType.QueuedConnection
        self._sig_state.connect(self._on_state, queued)
        self._sig_length.connect(self._apply_length, queued)
        self._sig_volume.connect(self.volume_changed, queued)
        self._sig_end.connect(self._on_end, queued)
        self._sig_error.connect(self._on_error, queued)
        self._sig_vout.connect(self._on_vout, queued)

        self._attach_events()

    # ------------------------------------------------------------- events
    def _attach_events(self) -> None:
        """libVLC event callbacks run on a VLC worker thread.

        Touching a QTimer from there is not allowed and fails silently
        ("Timers cannot be started from another thread"), which left the poll
        dead and the UI frozen at 0:00 while the file played. So every callback
        does the minimum work in the VLC thread and hands the value to the GUI
        thread through a queued signal; the slots below do all the QObject work.
        """
        manager = self._player.event_manager()

        def on(event_type, handler):
            manager.event_attach(event_type, handler)
            self._callbacks.append(handler)

        on(vlc.EventType.MediaPlayerPlaying, lambda _e: self._sig_state.emit(True))
        on(vlc.EventType.MediaPlayerPaused, lambda _e: self._sig_state.emit(False))
        on(vlc.EventType.MediaPlayerStopped, lambda _e: self._sig_state.emit(False))
        on(vlc.EventType.MediaPlayerEndReached, lambda _e: self._sig_end.emit())
        on(vlc.EventType.MediaPlayerEncounteredError, lambda _e: self._sig_error.emit())
        on(vlc.EventType.MediaPlayerLengthChanged, self._on_length)
        on(vlc.EventType.MediaPlayerAudioVolume, self._on_volume)
        on(vlc.EventType.MediaPlayerVout, lambda _e: self._sig_vout.emit())

    def _on_state(self, playing: bool) -> None:
        if self._released:
            return
        if playing:
            if not self._poll.isActive():
                self._poll.start()
        else:
            self._poll.stop()
        self.state_changed.emit(playing)

    def _on_length(self, event) -> None:
        try:
            length = int(event.u.new_length)
        except Exception:  # noqa: BLE001 - event union shape varies by version
            return
        self._sig_length.emit(length)  # hop to the GUI thread

    def _apply_length(self, length: int) -> None:
        if self._released:
            return
        if length > 0 and length != self._duration:
            self._duration = length
            self._video_reported = self._video_reported or self._has_video()
            self.opened.emit(self._duration, self._has_video())

    def _on_vout(self) -> None:
        """A video output appeared after we already reported the media.

        With `:start-paused` libVLC decodes the first frame and holds the clock;
        LengthChanged can arrive before the vout exists, so the window would keep
        its "open a file" hint over a perfectly good picture. Report once more,
        and only if we have never seen a picture for this media.
        """
        if self._released or self._duration <= 0 or self._video_reported:
            return
        if self._has_video():
            self._video_reported = True
            self.opened.emit(self._duration, True)

    def _has_video(self) -> bool:
        """python-vlc exposes no MediaPlayer.has_video(); the vout size is the honest probe."""
        try:
            width, height = self._player.video_get_size()
        except Exception:  # noqa: BLE001
            return False
        return bool(width) and bool(height)

    def _on_end(self) -> None:
        if self._released:
            return
        self._poll.stop()
        self.state_changed.emit(False)
        self.ended.emit()

    def _on_error(self) -> None:
        if self._released:
            return
        self._poll.stop()
        self.error.emit("libVLC could not play this media")

    def _on_volume(self, event) -> None:
        try:
            volume = int(event.u.volume)
        except Exception:  # noqa: BLE001
            return
        if volume >= 0:
            self._volume = volume  # keep the cache honest: audio_set_volume is not the only source
            self._sig_volume.emit(volume)  # hop to the GUI thread

    # ------------------------------------------------------------- polling
    def _on_poll(self) -> None:
        if time.monotonic() < self._seek_guard_until:
            return
        position = self._player.get_time()
        if position >= 0:
            self.position_changed.emit(int(position))

    # ------------------------------------------------------------- controls
    def set_hwnd(self, hwnd: int) -> None:
        self._hwnd = hwnd
        if hwnd:
            self._player.set_hwnd(hwnd)
            # VLC's vout creates its own child window inside that HWND and would
            # swallow mouse/wheel events over the video. Turning its input off
            # makes it forward them to the parent, which is our Qt widget.
            # Verified: without these two calls mouse+whell events never reach Qt.
            self._player.video_set_mouse_input(False)
            self._player.video_set_key_input(False)

    def open(
        self,
        path: str,
        autoplay: bool = True,
        subtitle: str | None = None,
        start_ms: int = 0,
    ) -> bool:
        if self._player is None or self._instance is None:
            self.error.emit("The player has been shut down.")
            return False
        source = Path(path)
        if not source.exists():
            self.error.emit(f"File not found: {source}")
            return False

        media = self._instance.media_new(str(source))
        if media is None:
            self.error.emit(f"libVLC could not open: {source.name}")
            return False

        if subtitle:
            # ":sub-file" is read while the input is being built, which is the
            # one path that works when add_slave() is refused mid-playback.
            media.add_option(f":sub-file={subtitle}")

        if start_ms > 0:
            # ":start-time" is applied while the input is built, so resuming is a
            # seek-free start instead of a seek right after play() (which the
            # seek guard and the demuxer would fight over).
            media.add_option(f":start-time={start_ms / 1000.0:.3f}")

        if not autoplay:
            # libVLC's own "open but hold": the first frame is decoded and shown,
            # the clock stays stopped. Beats play()+pause(), which races the
            # Opening state and can leak a frame of audio.
            media.add_option(":start-paused")

        self._media = media
        self._path = str(source)
        self._duration = 0
        self._video_reported = False
        self._player.set_media(media)
        self._player.play()  # MediaPlayerPlaying -> opened(duration) + state_changed
        return True

    def replay(self) -> bool:
        """Start the current file again from zero.

        Deliberately a re-open, not `seek_ms(0)` + `play()`: after EndReached
        libVLC has released the input and a stopped player does not reliably
        resume, which left the window stuck on the last frame with a dead play
        button. Re-opening is a fresh input at t=0 and always works.
        """
        if not self._path or self._player is None:
            return False
        return self.open(self._path)

    def play(self) -> None:
        if self._player is None:
            return
        if self._player.get_state() == vlc.State.Paused:
            self._player.set_pause(0)
        else:
            self._player.play()

    def pause(self) -> None:
        if self._player is not None and self._player.is_playing():
            self._player.set_pause(1)

    def toggle(self) -> None:
        if self._player is None:
            return
        if self._player.is_playing():
            self.pause()
        else:
            self.play()

    def stop(self) -> None:
        self._poll.stop()
        if self._player is not None:
            self._player.stop()

    def seek_ms(self, position_ms: int) -> None:
        if self._duration <= 0 or self._player is None:
            return
        position_ms = max(0, min(int(position_ms), self._duration))
        self._seek_guard_until = time.monotonic() + SEEK_GUARD_S
        self._player.set_time(position_ms)
        self.position_changed.emit(position_ms)

    def seek_by(self, delta_ms: int) -> None:
        self.seek_ms(self.position_ms() + delta_ms)

    # ------------------------------------------------------------- queries
    # Every query tolerates a released engine: after closeEvent the widgets can
    # still fire a late click or a queued poll, and a bare AttributeError on None
    # used to kill the close sequence.
    def is_playing(self) -> bool:
        return bool(self._player is not None and self._player.is_playing())

    def position_ms(self) -> int:
        if self._player is None:
            return 0
        value = self._player.get_time()
        return max(0, int(value))

    def duration_ms(self) -> int:
        if self._duration <= 0 and self._player is not None:
            value = self._player.get_length()
            self._duration = max(0, int(value))
        return self._duration

    def has_video(self) -> bool:
        return self._player is not None and self._has_video()

    # --------------------------------------------------------------- volume
    def volume(self) -> int:
        return self._volume

    def set_volume(self, volume: int) -> None:
        volume = max(0, min(int(volume), 100))
        self._volume = volume
        if self._player is not None:
            self._player.audio_set_volume(volume)
        self.volume_changed.emit(volume)

    def is_muted(self) -> bool:
        return self._muted

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)
        if self._player is not None:
            self._player.audio_set_mute(self._muted)
        self.muted_changed.emit(self._muted)

    # --------------------------------------------------------------- streams
    # Track lists are read on demand, never cached: libVLC reports them late (the
    # ES selections arrive with the first packets), so a cached list would show a
    # menu with the previous file's tracks or none at all.
    def audio_tracks(self) -> list[tuple[int, str]]:
        return [(tid, name) for tid, name in self._descriptions("audio") if tid >= 0]

    def subtitle_tracks(self) -> list[tuple[int, str]]:
        """Subtitles, without libVLC's synthetic "Disable" entry (id -1)."""
        return [(tid, name) for tid, name in self._descriptions("spu") if tid >= 0]

    def current_audio(self) -> int:
        return self._track("audio")

    def current_subtitle(self) -> int:
        return self._track("spu")

    def set_audio(self, track_id: int) -> None:
        if self._player is not None:
            self._player.audio_set_track(int(track_id))

    def set_subtitle(self, track_id: int) -> None:
        """-1 disables subtitles, which is what libVLC itself uses."""
        if self._player is not None:
            self._player.video_set_spu(int(track_id))

    def load_subtitle(self, path: str) -> bool:
        """Ask libVLC to attach a subtitle file to the running input.

        The path has to go in as an MRL. A bare Windows path returns success and
        then adds *nothing* - that is the whole reason dropped subtitles used to
        look loaded and never appear.
        """
        if self._player is None or self._duration <= 0:
            return False
        try:
            uri = Path(path).resolve().as_uri()
            return int(self._player.add_slave(vlc.MediaSlaveType.subtitle, uri, True)) == 0
        except Exception:  # noqa: BLE001 - a bad file must not kill the player
            return False

    def attach_subtitle(self, path: str) -> bool:
        """Attach a subtitle and select it as soon as libVLC registers it.

        `add_slave()` returns before the new elementary stream shows up in the
        track list, and the track is *not* selected automatically - an unselected
        subtitle looks exactly like a failed load. Worse, the add is occasionally
        dropped when it races the input's ES setup, so it is retried a few times
        before giving up. `subtitle_attached(bool)` reports the outcome.
        """
        before = frozenset(track_id for track_id, _ in self.subtitle_tracks())
        if not self.load_subtitle(path):
            return False
        self._sub_pending = (before, _SUB_POLL_TRIES, str(path))
        QTimer.singleShot(_SUB_POLL_MS, self._resolve_pending_subtitle)
        return True

    def _resolve_pending_subtitle(self) -> None:
        pending = self._sub_pending
        if pending is None:
            return
        before, tries, path = pending
        added = [track_id for track_id, _ in self.subtitle_tracks() if track_id not in before]
        if added:
            self._sub_pending = None
            self.set_subtitle(added[-1])
            self.subtitle_attached.emit(True)
            return
        if tries <= 1:
            self._sub_pending = None
            self.subtitle_attached.emit(False)
            return
        self.load_subtitle(path)  # the previous add was dropped, not refused
        self._sub_pending = (before, tries - 1, path)
        QTimer.singleShot(_SUB_POLL_MS, self._resolve_pending_subtitle)

    def subtitle_delay(self) -> int:
        """Current subtitle delay in milliseconds; positive = subtitles later."""
        if self._player is None:
            return 0
        try:
            return int(self._player.video_get_spu_delay()) // 1000  # µs -> ms
        except Exception:  # noqa: BLE001
            return 0

    def set_subtitle_delay(self, milliseconds: int) -> None:
        if self._player is None or self._duration <= 0:
            return
        try:
            self._player.video_set_spu_delay(int(milliseconds) * 1000)
        except Exception:  # noqa: BLE001 - no input yet, or no subtitle track
            return

    def current_path(self) -> str | None:
        return self._path

    def _descriptions(self, kind: str) -> list[tuple[int, str]]:
        if self._player is None:
            return []
        try:
            if kind == "audio":
                raw = self._player.audio_get_track_description()
            else:
                raw = self._player.video_get_spu_description()
        except Exception:  # noqa: BLE001
            return []
        tracks: list[tuple[int, str]] = []
        for entry in raw or []:
            try:
                track_id, name = entry
            except (TypeError, ValueError):
                continue
            text = name.decode("utf-8", "replace") if isinstance(name, bytes) else str(name)
            tracks.append((int(track_id), text or f"Track {track_id}"))
        return tracks

    def _track(self, kind: str) -> int:
        if self._player is None:
            return -1
        try:
            if kind == "audio":
                return int(self._player.audio_get_track())
            return int(self._player.video_get_spu())
        except Exception:  # noqa: BLE001
            return -1

    def has_media(self) -> bool:
        """True once a file is open and its duration is known."""
        return self._media is not None and self._duration > 0

    def cycle_audio(self) -> int:
        """Next audio track, wrapping around. Returns the id now active (-1 none)."""
        tracks = [track_id for track_id, _ in self.audio_tracks()]
        if not tracks:
            return -1
        current = self.current_audio()
        index = tracks.index(current) + 1 if current in tracks else 0
        nxt = tracks[index % len(tracks)]
        self.set_audio(nxt)
        return nxt

    def cycle_subtitle(self) -> int:
        """Off -> track 1 -> track 2 -> ... -> Off, like VLC's subtitle key."""
        tracks = [track_id for track_id, _ in self.subtitle_tracks()]
        order = [-1, *tracks]
        current = self.current_subtitle()
        index = order.index(current) + 1 if current in order else 0
        nxt = order[index % len(order)]
        self.set_subtitle(nxt)
        return nxt

    def audio_delay(self) -> int:
        """Audio delay in milliseconds; positive = sound later."""
        if self._player is None:
            return 0
        try:
            return int(self._player.audio_get_delay()) // 1000  # µs -> ms
        except Exception:  # noqa: BLE001
            return 0

    def set_audio_delay(self, milliseconds: int) -> None:
        if self._player is None or self._duration <= 0:
            return
        try:
            self._player.audio_set_delay(int(milliseconds) * 1000)
        except Exception:  # noqa: BLE001
            return

    # ------------------------------------------------------ speed and aspect
    def rate(self) -> float:
        if self._player is None:
            return 1.0
        return float(self._player.get_rate()) or 1.0

    def set_rate(self, rate: float) -> None:
        if self._player is not None:
            self._player.set_rate(float(rate))

    def aspect(self) -> str:
        if self._player is None:
            return ""
        value = self._player.video_get_aspect_ratio()
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        return (value or "").strip()

    def set_aspect(self, ratio: str) -> None:
        """`""` means "let the file decide" - the only way back from 16:9."""
        if self._player is not None:
            self._player.video_set_aspect_ratio(ratio or None)

    def crop(self) -> str:
        if self._player is None:
            return ""
        value = self._player.video_get_crop_geometry()
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        return (value or "").strip()

    def set_crop(self, ratio: str) -> None:
        """Crop away letterboxing/matte; `""` restores the full frame."""
        if self._player is not None:
            self._player.video_set_crop_geometry(ratio or "")

    def scale(self) -> float:
        if self._player is None:
            return 0.0
        return float(self._player.video_get_scale())

    def set_scale(self, factor: float) -> None:
        """0.0 = fit the window; anything else is a fixed zoom factor."""
        if self._player is not None:
            self._player.video_set_scale(float(factor))

    # -------------------------------------------------------------- teardown
    def release(self) -> None:
        """Tear libVLC down.

        `_released` matters more than it looks: player.stop() makes libVLC emit
        StateChanged(Stopped) from its worker thread, and that queued signal was
        delivered *after* the window closed - which called overlay.wake() and left
        a floating control strip on the desktop.
        """
        if self._released:
            return
        self._released = True
        self._poll.stop()
        self._callbacks.clear()
        player, instance = self._player, self._instance
        self._player, self._instance = None, None  # type: ignore[assignment]
        try:
            if player is not None:
                player.stop()
                player.release()
        finally:
            if instance is not None:
                instance.release()
