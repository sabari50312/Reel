"""Where the sound is: a loudness map for the seek bar.

The map answers one question - "is there sound at this point of the file?" - so
the seek bar can show the shape of the audio without the user having to listen
for it. That is what makes lining subtitles up by eye possible.

Why loudness and nothing else: two speech detectors were tried and measured.
Silero VAD returned a median probability of 0.007 on clearly-spoken film audio
(it scored 54 % on synthetic speech), and WebRTC VAD marks 95 % of pure music as
voice. Neither is trustworthy on real mixes, and a map that is confidently wrong
is worse than one that only claims "sound here".

How it is computed:

  * PyAV decodes the chosen audio stream with no clock in the loop - measured
    ~1300x real time, so a two-hour film lands in a few seconds.
  * Every 30 ms chunk is measured with `audioop.rms`; 250 ms buckets store the
    mean level. Levels are normalised against the 95th percentile so a single
    explosion cannot flatten the rest of the file.
  * Buckets are placed by *time* (`bucket * BUCKET_S`), never by index: an audio
    track is often shorter than the video (credits are silent), and stretching
    the index space across the rail drifts by seconds.

Decoding through PyAV rather than libVLC is deliberate: libVLC's transcode
pipeline is slightly faster still but binds to whichever audio stream announces
first, so it cannot analyse track 2 of a dual-audio file.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Signal

from .settings import cache_dir

try:  # C implementation, ~50x faster than a Python loop
    import audioop  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - only if audioop leaves the stdlib
    audioop = None  # type: ignore[assignment]

def _import_av():
    """Import PyAV, putting its bundled FFmpeg DLLs on the search path first.

    PyAV ships those DLLs in `av.libs` beside the package, and its `__init__`
    registers that folder - but a frozen build never runs that code, so the
    import failed silently there and every film showed "no sound map". Both
    layouts are covered here: next to the package (installed) and next to the
    executable (frozen).
    """
    if sys.platform == "win32":
        candidates = [Path(sys.executable).resolve().parent / "av.libs"]
        spec = importlib.util.find_spec("av")
        if spec is not None and spec.origin:
            candidates.append(Path(spec.origin).resolve().parent.parent / "av.libs")
        for candidate in candidates:
            if candidate.is_dir():
                try:
                    os.add_dll_directory(str(candidate))
                except OSError:
                    pass
    global _av_error
    try:
        import av as module  # type: ignore[import-not-found]
    except Exception as error:  # noqa: BLE001 - record and degrade
        _av_error = repr(error)
        return None
    return module

BUCKET_S = 0.25  # one band per quarter second: fine enough to show a pause
SAMPLE_RATE = 16_000
FRAME_MS = 30
BYTES_PER_SAMPLE = 2  # S16 mono
FRAME_BYTES = int(SAMPLE_RATE * FRAME_MS / 1000) * BYTES_PER_SAMPLE
_CEILING_PERCENTILE = 0.95  # ignore the loudest 5 % when scaling
_UNKNOWN = -1  # a bucket the pass never reached
_REPORT_S = 0.5  # progress cadence, in seconds of wall clock

_DETECTOR_VERSION = 12  # bump whenever the numbers would change meaning

STORE_GROUP = "audio_map"
STORE_LIMIT = 40

_log_paths: list[Path] = []


def _log_targets() -> list[Path]:
    """Where debug lines go: beside the project and in the app data folder."""
    global _log_paths
    if _log_paths:
        return _log_paths
    targets = [Path(__file__).resolve().parents[2] / "reel.log"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        targets.append(Path(local) / "Reel" / "reel.log")
    _log_paths = targets
    return targets


def _log(message: str) -> None:
    line = f"[reel] {message}"
    print(line, flush=True)  # noqa: T201 - diagnostics, not user output
    for target in _log_targets():
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%H:%M:%S')} {line}\n")
        except OSError:
            continue


_av_error: str | None = None

_cache: dict[tuple[str, int | None], list[int]] = {}
_cache_lock = threading.Lock()


class AudioMapError(Exception):
    """Raised when the file has no decodable audio track."""


def _key(path: str, track_index: int | None) -> str:
    resolved = os.path.normcase(os.path.abspath(path))
    stamp = f"{resolved}|track={track_index}|v={_DETECTOR_VERSION}"
    return hashlib.sha1(stamp.encode("utf-8")).hexdigest()


def _stat_of(path: str) -> str:
    try:
        info = os.stat(path)
    except OSError:
        return ""
    return f"{info.st_size}:{int(info.st_mtime)}"


av = _import_av()
if av is None:
    _log(f"map: PyAV unavailable ({_av_error})")


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


class _Buckets:
    """Accumulates chunks into 250 ms loudness buckets."""

    def __init__(self) -> None:
        self._total = bytearray()
        self._sum = 0.0
        self._frames = 0
        self.levels: list[int] = []

    def add(self, chunk: bytes) -> None:
        self._total += chunk
        self._sum += _rms(chunk) * len(chunk)
        self._frames += len(chunk)
        limit = int(SAMPLE_RATE * BUCKET_S) * BYTES_PER_SAMPLE
        while len(self._total) >= limit:
            piece = bytes(self._total[:limit])
            del self._total[:limit]
            self.levels.append(int(round(_rms(piece))))

    def close(self) -> list[int]:
        if self._total:
            self.levels.append(int(round(_rms(bytes(self._total)))))
            self._total.clear()
        return self.levels


def _normalise(levels: list[int]) -> list[int]:
    if not levels:
        return []
    ordered = sorted(levels)
    ceiling = ordered[min(len(ordered) - 1, int(len(ordered) * _CEILING_PERCENTILE))]
    if ceiling <= 0:
        return [0] * len(levels)
    return [max(0, min(100, int(round(value / ceiling * 100.0)))) for value in levels]


def _mean_level(levels: list[int]) -> float:
    if not levels:
        return 0.0
    return sum(levels) / len(levels) / 32768.0


def _analyze_with_av(
    path: str,
    track_index: int | None,
    cancel: threading.Event | None,
    progress,
    tick,
) -> list[int] | None:
    """Decode the stream as fast as the CPU allows and measure each bucket."""
    if av is None:  # pragma: no cover - dependency is declared
        return None
    try:
        container = av.open(path)
    except Exception as error:  # noqa: BLE001 - any demux failure means "no map"
        _log(f"map open failed: {error}")
        return None

    try:
        streams = list(container.streams.audio)
        if not streams:
            raise AudioMapError("No audio track to analyse.")
        index = 0 if track_index is None else max(0, min(int(track_index), len(streams) - 1))
        stream = streams[index]

        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        buckets = _Buckets()
        pending = bytearray()  # resampled pieces are not frame-sized
        total_s = float(stream.duration * stream.time_base) if stream.duration else 0.0
        if total_s <= 0 and container.duration:
            total_s = float(container.duration) / 1_000_000.0
        clock = 0.0
        started = False
        reported = 0.0
        logged_decile = -1
        _log(
            f"map decode: stream={index} codec={stream.codec_context.name} "
            f"rate={stream.codec_context.sample_rate} "
            f"layout={stream.codec_context.layout.name} duration={total_s:.1f}s"
        )

        for frame in container.decode(stream):
            if cancel is not None and cancel.is_set():
                return None
            if not started and frame.time is not None:
                clock = float(frame.time)
                started = True
            for piece in resampler.resample(frame):
                data = bytes(piece.planes[0])
                data = data[: piece.samples * BYTES_PER_SAMPLE]
                pending += data
                while len(pending) >= FRAME_BYTES:
                    buckets.add(bytes(pending[:FRAME_BYTES]))
                    del pending[:FRAME_BYTES]
                clock += piece.samples / SAMPLE_RATE
            now = time.monotonic()
            if total_s > 0 and now - reported >= _REPORT_S:
                reported = now
                fraction = min(0.99, max(0.0, clock / total_s))
                if tick is not None:
                    tick(fraction)
                if progress is not None:
                    progress(fraction, clock, len(buckets.levels))
                decile = int(fraction * 10)
                if decile > logged_decile:
                    logged_decile = decile
                    _log(f"map progress: {fraction:.0%} of {total_s:.0f}s")
    finally:
        container.close()

    levels = buckets.close()
    if not levels:
        raise AudioMapError("No audio track to analyse.")
    _log(
        f"map stats: {len(levels)} buckets, mean level "
        f"{_mean_level(levels):.4f} of full scale, clock {clock:.0f}s"
    )
    return _normalise(levels)


def analyze(
    path: str,
    cancel: threading.Event | None = None,
    progress=None,
    track_index: int | None = None,
    tick=None,
) -> list[int]:
    """Return 0..100 loudness per `BUCKET_S` bucket for the file's audio track."""
    source = Path(path)
    if not source.is_file():
        raise AudioMapError("File not found.")

    levels = _analyze_with_av(str(source), track_index, cancel, progress, tick)
    if levels is None:
        raise AudioMapError("Could not decode this file's audio.")
    if tick is not None:
        tick(1.0)
    return levels


class AudioMapStore:
    """Tiny cache of loudness maps, keyed by path with size+mtime for validity."""

    def __init__(self) -> None:
        # A cache, so it does not live with the settings: it can be thrown away
        # and rebuilt, and a few megabytes of levels have no business in the INI
        # a user might open and read.
        path = cache_dir() / "audiomap.ini"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # read-only profile: the cache simply never hits
        self._store = QSettings(str(path), QSettings.Format.IniFormat)
        self._store.beginGroup(STORE_GROUP)

    def get(self, path: str, track_index: int | None = None) -> list[int] | None:
        stamp = _stat_of(path)
        if not stamp:
            return None
        self._store.beginGroup(_key(path, track_index))
        stored = self._store.value("stat", "")
        raw = self._store.value("levels", "")
        self._store.endGroup()
        if stored != stamp or not raw:
            return None
        try:
            return [int(piece) for piece in str(raw).split(",") if piece != ""]
        except ValueError:
            return None

    def put(self, path: str, levels: list[int], track_index: int | None = None) -> None:
        self._store.beginGroup(_key(path, track_index))
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
    progress = Signal(str, object)
    analysing = Signal(str, float)  # fraction 0..1, negative when it stops
    summary = Signal(str, str)  # short human note for the settings panel

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._store = AudioMapStore()
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        self._generation = 0

    def cached(self, path: str, track_index: int | None = None) -> list[int] | None:
        with _cache_lock:
            hit = _cache.get((path, track_index))
        if hit is not None:
            return hit
        stored = self._store.get(path, track_index)
        if stored:
            with _cache_lock:
                _cache[(path, track_index)] = stored
        return stored

    def request(self, path: str, track_index: int | None = None) -> list[int] | None:
        """Return a cached map immediately, otherwise start analysing."""
        if not path:
            return None
        hit = self.cached(path, track_index)
        if hit is not None:
            voiced = 100.0 * sum(1 for level in hit if level >= 22) / max(1, len(hit))
            _log(
                f"map request: {os.path.basename(path)} track={track_index} cached=yes "
                f"buckets={len(hit)} sound={voiced:.0f}%"
            )
            return hit
        _log(f"map request: {os.path.basename(path)} track={track_index} cached=no")
        self.cancel()
        self._generation += 1
        generation = self._generation
        cancel = threading.Event()
        self._cancel = cancel

        def work() -> None:
            _log("map start")
            try:
                levels = analyze(
                    path,
                    cancel,
                    track_index=track_index,
                    tick=lambda fraction: self.analysing.emit(path, fraction),
                )
            except Exception as error:  # noqa: BLE001 - no audio, bad file, cancelled
                _log(f"map failed: {error!r}")
                self.analysing.emit(path, -1.0)
                self.summary.emit(path, "No sound map for this file")
                return
            if cancel.is_set() or generation != self._generation:
                _log("map abandoned: newer request")
                return
            with _cache_lock:
                _cache[(path, track_index)] = levels
            self._store.put(path, levels, track_index)
            sound = 100.0 * sum(1 for level in levels if level >= 22) / max(1, len(levels))
            _log(f"map ready: {len(levels)} buckets, sound {sound:.0f}%")
            self.analysing.emit(path, -1.0)
            self.summary.emit(path, f"{len(levels)} bands, {sound:.0f}% with sound")
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
