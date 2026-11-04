"""Where the sound is: a loudness map for the seek bar.

The point is to *see* speech/music regions without being told what they are, so
the bar carries a texture of soft green bands instead of a legend.

How it works, and why this way:

  * libVLC can be told to transcode the audio track to a file (`:sout=` with
    `#transcode{acodec=s16l,channels=1,samplerate=16000}:std{access=file,mux=wav}`).
    A sout pipeline runs as fast as the decoder allows instead of in real time -
    measured 77x for a 30 s clip, versus 1.0x when pulling PCM through audio
    callbacks, which would have made a two-hour film a two-hour analysis.
  * The WAV is one channel at 16 kHz, so a quarter-second bucket is 8 KiB: the
    whole file is scanned with `audioop.rms` in C, then deleted.
  * Cache is keyed by path + size + mtime, so reopening a video paints the map
    instantly instead of decoding it again.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import tempfile
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Signal

try:  # C implementation, ~50x faster than a Python loop
    import audioop  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - only if audioop leaves the stdlib
    audioop = None  # type: ignore[assignment]

import vlc

BUCKET_S = 0.25  # one band per quarter second: fine enough to show pauses
SAMPLE_RATE = 16_000
BYTES_PER_SAMPLE = 2  # S16N mono
_CEILING_PERCENTILE = 0.95  # ignore the loudest 5% so one explosion cannot flatten the map
_TRACK_RATE = 4.0  # fastest callback decoding that does not starve the aout
_UNKNOWN = -1  # bucket not analysed yet
_DEADLINE_FACTOR = 1.8  # generous headroom over the measured ~77x decode speed
_DEADLINE_FLOOR_S = 8.0
_DEADLINE_CEILING_S = 240.0
STORE_GROUP = "audio_map"
STORE_LIMIT = 40

_cache: dict[str, list[int]] = {}
_cache_lock = threading.Lock()


class AudioMapError(Exception):
    """Raised when the file has no decodable audio track."""


def _key(path: str, track_id: int | None = None) -> str:
    resolved = os.path.normcase(os.path.abspath(path))
    suffix = f"|track={int(track_id)}" if track_id is not None else ""
    return hashlib.sha1((resolved + suffix).encode("utf-8")).hexdigest()


def _stat_of(path: str) -> str:
    try:
        info = os.stat(path)
    except OSError:
        return ""
    return f"{info.st_size}:{int(info.st_mtime)}"


def _rms(fragment: bytes) -> float:
    if not fragment:
        return 0.0
    if audioop is not None:
        return float(audioop.rms(fragment, BYTES_PER_SAMPLE))
    total = 0
    count = len(fragment) // BYTES_PER_SAMPLE
    for index in range(count):
        value = int.from_bytes(fragment[index * 2 : index * 2 + 2], "little", signed=True)
        total += value * value
    return float((total / max(1, count)) ** 0.5)


def _decode_to_wav(path: str, cancel: threading.Event | None, tick=None) -> tuple[str, float]:
    """Ask libVLC for the audio track only, as fast as it can decode it."""
    handle, wav = tempfile.mkstemp(prefix="reel-audio-", suffix=".wav")
    os.close(handle)
    os.unlink(wav)  # VLC wants to create it
    instance = vlc.Instance("--intf=dummy", "--no-video", "--quiet")
    if instance is None:  # pragma: no cover - only if libVLC is missing
        raise AudioMapError("libVLC failed to start.")
    player = instance.media_player_new()
    try:
        media = instance.media_new_path(str(path))
        media.add_option(
            f":sout=#transcode{{acodec=s16l,channels=1,samplerate={SAMPLE_RATE}}}"
            f":std{{access=file,mux=wav,dst={wav}}}"
        )
        player.set_media(media)
        if player.play() != 0:
            raise AudioMapError("libVLC could not start this file.")
        started = time.monotonic()
        deadline = started + _DEADLINE_CEILING_S
        ticked = 0.0
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                break
            state = player.get_state()
            if state in (vlc.State.Ended, vlc.State.Error, vlc.State.Stopped):
                break
            length = player.get_length()
            if length > 0:
                # Length is known: the transcode runs far faster than playback,
                # so a proportional deadline is plenty and keeps a stuck decoder
                # from pinning a thread forever.
                estimate = length / 1000.0 / 60.0 * _DEADLINE_FACTOR
                deadline = min(deadline, started + max(_DEADLINE_FLOOR_S, estimate))
                if tick is not None and time.monotonic() - ticked > 0.2:
                    # Real progress: the transcode writes the same timeline it
                    # demuxes, so decoded time over length is the fraction done.
                    ticked = time.monotonic()
                    tick(min(1.0, max(0.0, player.get_time() / float(length))))
            time.sleep(0.02)
        took = time.monotonic() - started
    finally:
        try:
            player.stop()
        except Exception:  # noqa: BLE001
            pass
        player.release()
        instance.release()
    return wav, took


def _probe_duration(path: str, cancel: threading.Event | None) -> int:
    """Length in ms, read without starting playback."""
    instance = vlc.Instance("--intf=dummy", "--no-video", "--aout=dummy", "--quiet")
    if instance is None:  # pragma: no cover
        return 0
    player = instance.media_player_new()
    try:
        player.set_media(instance.media_new_path(path))
        player.play()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                return 0
            length = player.get_length()
            if length > 0:
                return int(length)
            time.sleep(0.02)
        return 0
    finally:
        try:
            player.stop()
        except Exception:  # noqa: BLE001
            pass
        player.release()
        instance.release()


def _read_samples(wav: str) -> bytes:
    try:
        data = Path(wav).read_bytes()
    except OSError:
        return b""
    marker = data.find(b"data")
    if marker < 0:
        return b""
    return data[marker + 8 :]


def _analyze_track(
    path: str,
    track_id: int,
    duration_ms: int,
    cancel: threading.Event | None,
    progress,
    base: list[int] | None = None,
) -> list[int]:
    """Loudness of one specific audio track, decoded at 6x real time.

    The fast transcode path cannot be told which track to take: libVLC 3 binds the
    transcode chain to whichever elementary stream reports first, so on a file with
    several audio tracks the map silently follows an arbitrary one (measured: the
    same command picked different tracks on different runs). Playback callbacks
    *are* deterministic - `audio_set_track()` is honoured - so a multi-track file
    is decoded through those instead, six times faster than real time, reporting
    buckets as they complete. Seeking stalls a callback-based aout, so the pass
    runs straight through from the start.
    """
    total_buckets = max(1, int(duration_ms / 1000.0 / BUCKET_S))
    rms_by_bucket: dict[int, float] = {}
    position = {"content": 0.0, "rate": _TRACK_RATE}

    _PlayCb = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_int64)
    _VoidCb = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int64)

    def on_play(_data, samples, count, _pts) -> None:
        if not samples or not count:
            return
        span = int(count) * position["rate"] / SAMPLE_RATE
        bucket = int(position["content"] / BUCKET_S)
        if 0 <= bucket < total_buckets:
            rms_by_bucket.setdefault(bucket, _rms(ctypes.string_at(samples, int(count) * BYTES_PER_SAMPLE)))
        position["content"] += span

    play_cb = _PlayCb(on_play)
    noop = _VoidCb(lambda _data, _pts: None)

    def wrap(callback, kind):  # python-vlc wants its own CFUNCTYPE subclasses
        return kind(ctypes.cast(callback, ctypes.c_void_p).value)

    instance = vlc.Instance("--intf=dummy", "--no-video", "--aout=dummy", "--quiet")
    if instance is None:  # pragma: no cover
        raise AudioMapError("libVLC failed to start.")
    player = instance.media_player_new()
    levels = [_UNKNOWN] * total_buckets
    try:
        player.audio_set_format(b"S16N", SAMPLE_RATE, 1)
        player.audio_set_callbacks(
            wrap(play_cb, vlc.AudioPlayCb),
            wrap(noop, vlc.AudioPauseCb),
            wrap(noop, vlc.AudioResumeCb),
            wrap(noop, vlc.AudioFlushCb),
            wrap(noop, vlc.AudioDrainCb),
            None,
        )
        player.set_media(instance.media_new_path(str(path)))
        if player.play() != 0:
            raise AudioMapError("libVLC could not start this file.")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and player.get_length() <= 0:
            if cancel is not None and cancel.is_set():
                return levels
            time.sleep(0.02)
        player.audio_set_track(int(track_id))
        player.set_rate(_TRACK_RATE)

        next_report = time.monotonic() + 0.3
        while True:
            if cancel is not None and cancel.is_set():
                break
            if player.get_state() in (vlc.State.Ended, vlc.State.Error, vlc.State.Stopped):
                break
            time.sleep(0.05)
            if time.monotonic() >= next_report:
                next_report = time.monotonic() + 0.3
                levels = _fill(levels, rms_by_bucket, total_buckets, base)
                if progress is not None:
                    progress(list(levels))
    finally:
        try:
            player.stop()
        except Exception:  # noqa: BLE001
            pass
        player.release()
        instance.release()
    return _fill(levels, rms_by_bucket, total_buckets, base)


def _fill(
    levels: list[int],
    rms_by_bucket: dict[int, float],
    total: int,
    base: list[int] | None = None,
) -> list[int]:
    """Normalise what has been measured so far, leaving the rest as _UNKNOWN."""
    if not rms_by_bucket:
        return list(base) if base else [_UNKNOWN] * total
    known = sorted(rms_by_bucket)
    normalised = _normalise([rms_by_bucket[key] for key in known])
    if base:
        # The provisional (transcode) map can be a bucket or two shorter than the
        # duration-derived count, so resample instead of throwing it away -
        # otherwise the waveform blanks out the moment the accurate pass starts.
        filled = [base[min(len(base) - 1, int(index * len(base) / total))] for index in range(total)]
    else:
        filled = [_UNKNOWN] * total
    for key, value in zip(known, normalised):
        if 0 <= key < total:
            filled[key] = value
    return filled


def analyze(
    path: str,
    cancel: threading.Event | None = None,
    progress=None,
    track_id: int | None = None,
    tick=None,
) -> list[int]:
    """Return 0..100 loudness per `BUCKET_S` bucket for the file's audio track."""
    source = Path(path)
    if not source.is_file():
        raise AudioMapError("File not found.")

    if track_id is not None:
        duration = _probe_duration(str(source), cancel)
        if duration > 0:
            # Show a whole-file map straight away (the transcode runs at ~77x but
            # cannot be pinned to a track), then correct it with the active track.
            provisional = base = None
            if progress is not None:
                provisional = _analyze_fast(str(source), cancel, tick)
                progress(list(provisional))
                base = provisional
            return _analyze_track(str(source), track_id, duration, cancel, progress, base)

    levels = _analyze_fast(str(source), cancel, tick)
    if tick is not None:
        tick(1.0)
    return levels


def _analyze_fast(path: str, cancel: threading.Event | None, tick=None) -> list[int]:
    source = Path(path)
    wav = ""
    try:
        wav, _took = _decode_to_wav(str(source), cancel, tick)
        samples = _read_samples(wav)
    finally:
        if wav:
            try:
                os.unlink(wav)
            except OSError:
                pass

    bucket_bytes = int(SAMPLE_RATE * BUCKET_S) * BYTES_PER_SAMPLE
    if not samples or len(samples) < bucket_bytes // 2:
        raise AudioMapError("No audio track to analyse.")

    levels = [
        _rms(samples[offset : offset + bucket_bytes])
        for offset in range(0, len(samples) - bucket_bytes + 1, bucket_bytes)
    ]
    if not levels:
        raise AudioMapError("No audio track to analyse.")
    return _normalise(levels)


def _normalise(levels: list[float]) -> list[int]:
    ordered = sorted(levels)
    ceiling = ordered[min(len(ordered) - 1, int(len(ordered) * _CEILING_PERCENTILE))]
    if ceiling <= 0:
        return [0] * len(levels)
    return [max(0, min(100, int(round(value / ceiling * 100.0)))) for value in levels]


class AudioMapStore:
    """Tiny cache of loudness maps, keyed by path with size+mtime for validity."""

    def __init__(self) -> None:
        self._store = QSettings()
        self._store.beginGroup(STORE_GROUP)

    def get(self, path: str, track_id: int | None = None) -> list[int] | None:
        stamp = _stat_of(path)
        if not stamp:
            return None
        self._store.beginGroup(_key(path, track_id))
        stored = self._store.value("stat", "")
        raw = self._store.value("levels", "")
        self._store.endGroup()
        if stored != stamp or not raw:
            return None
        try:
            return [int(piece) for piece in str(raw).split(",") if piece != ""]
        except ValueError:
            return None

    def put(self, path: str, levels: list[int], track_id: int | None = None) -> None:
        self._store.beginGroup(_key(path, track_id))
        self._store.setValue("stat", _stat_of(path))
        self._store.setValue("levels", ",".join(str(level) for level in levels))
        self._store.setValue("at", int(time.time()))
        self._store.endGroup()
        self._trim()

    def _trim(self) -> None:
        keys = self._store.childGroups()
        if len(keys) <= STORE_LIMIT:
            return
        stamps = []
        for key in keys:
            self._store.beginGroup(key)
            stamps.append((int(self._store.value("at", 0)), key))
            self._store.endGroup()
        for _stamp, key in sorted(stamps)[: len(keys) - STORE_LIMIT]:
            self._store.remove(key)


class AudioMapService(QObject):
    """Off-thread analyser: `ready(path, levels)` once a map exists."""

    ready = Signal(str, object)
    progress = Signal(str, object)  # partial map while a long file is analysed
    analysing = Signal(str, float)  # 0..1 decode progress, for a visible indicator

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._store = AudioMapStore()
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        self._generation = 0

    def cached(self, path: str, track_id: int | None = None) -> list[int] | None:
        key = _key(path, track_id)
        with _cache_lock:
            hit = _cache.get(key)
        if hit is not None:
            return hit
        stored = self._store.get(path, track_id)
        if stored:
            with _cache_lock:
                _cache[key] = stored
        return stored

    def request(self, path: str, track_id: int | None = None) -> list[int] | None:
        """Return a cached map immediately, otherwise start analysing.

        `track_id` selects a specific audio track; pass None for a file with a
        single track (the fast transcode path cannot be pinned to a track).
        """
        if not path:
            return None
        hit = self.cached(path, track_id)
        if hit is not None:
            return hit
        self.cancel()
        self._generation += 1
        generation = self._generation
        cancel = threading.Event()
        self._cancel = cancel

        key = _key(path, track_id)

        def work() -> None:
            def report(partial: list[int]) -> None:
                if not cancel.is_set() and generation == self._generation:
                    self.progress.emit(path, partial)

            def tick(fraction: float) -> None:
                if not cancel.is_set() and generation == self._generation:
                    self.analysing.emit(path, float(fraction))

            try:
                levels = analyze(path, cancel, progress=report, track_id=track_id, tick=tick)
            except Exception:  # noqa: BLE001 - no audio, bad file, cancelled
                if not cancel.is_set() and generation == self._generation:
                    self.analysing.emit(path, -1.0)  # tells the UI to drop the hint
                return
            if cancel.is_set() or generation != self._generation:
                return
            with _cache_lock:
                _cache[key] = levels
            self._store.put(path, levels, track_id)
            self.ready.emit(path, levels)

        self._worker = threading.Thread(target=work, name="reel-audiomap", daemon=True)
        self._worker.start()
        return None

    def cancel(self) -> None:
        self._cancel.set()


__all__ = [
    "AudioMapError",
    "AudioMapService",
    "AudioMapStore",
    "BUCKET_S",
    "analyze",
]
