r"""Persistent preferences, behind a typed facade.

Two rules that stop this module from leaking into the rest of the app:

  1. **Nobody else touches QSettings.** Callers ask for `settings.get("volume")`
     and get an int, or call `settings.set("volume", 80)` and get it clamped. Keys,
     defaults and limits all live in `SPECS`, one place to read.
  2. **Storage is decided at runtime, not compiled in.** Installed builds keep a
     per-user INI at `%APPDATA%\Reel\settings.ini`; a build with a file named
     `reel.portable` next to the exe keeps everything in `reel.ini` beside it. Same
     code either way, so the portable/installable question is settled by packaging.

No JSON, no registry: QSettings gives type conversion, atomic-ish writes and INI
files for free, and a plain file is something a user can find, read and delete.
Values are read back through `value(key, default)`, so a hand-edited or corrupted
file can only ever produce a default, never a crash.

Live-apply: `changed(key)` fires after a value really changed, and only then.
That is what the settings UI will listen to, so no OK/Apply buttons are needed.
"""

from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, QTimer, Signal

from .paths import local_app_dir

PORTABLE_MARKER = "reel.portable"
PORTABLE_INI = "reel.ini"
SETTINGS_DIR = "Reel"
SETTINGS_INI = "settings.ini"
# Where 0.0.1 kept its settings. Read once, copied into the INI, never touched
# again: an upgrade that deletes user data is worse than a stale key.
LEGACY_REGISTRY = r"HKEY_CURRENT_USER\Software\Reel\Reel"
# Caches that ended up in the same key are large and rebuild themselves, so they
# are the one thing worth leaving behind rather than copying.
_LEGACY_SKIP = ("audio_map/",)

SettingValue = bool | int | str | list[str]

SAVE_DEBOUNCE_MS = 250


@dataclass(frozen=True)
class Spec:
    """One preference: its key, its default, and (for numbers) its limits."""

    key: str
    default: SettingValue
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str, ...] | None = None  # only for str: an enum, not free text


def _spec(
    key: str,
    default: SettingValue,
    minimum: int | None = None,
    maximum: int | None = None,
    choices: tuple[str, ...] | None = None,
) -> Spec:
    return Spec(key, default, minimum, maximum, choices)


# Window defaults are only used on first run; after that the saved rect wins
# (unless it no longer lands on any screen - see MainWindow._restore_geometry).
SPECS: tuple[Spec, ...] = (
    _spec("volume", 100, 0, 100),
    _spec("muted", False),
    _spec("window_x", 0),
    _spec("window_y", 0),
    _spec("window_w", 1280, 320, 16384),
    _spec("window_h", 720, 180, 16384),
    _spec("window_maximized", False),
    # Playback behaviour
    _spec("play.autoplay", True),
    _spec("play.step_s", 5, 1, 60),
    _spec("play.short_step_s", 10, 1, 60),
    _spec("play.long_step_s", 30, 5, 300),
    _spec("sub.step_ms", 50, 10, 500),
    # Subtitle appearance. libVLC 3 has no live styling API, so these travel as
    # media options when an input is created (see MainWindow._subtitle_options).
    _spec("sub.text_scale", 100, 50, 300),
    _spec("sub.margin_pct", 6, 0, 45),
    _spec("sub.background", 0, 0, 100),
    _spec("sub.outline", "normal", choices=("none", "thin", "normal", "thick")),
    _spec("audio.step_ms", 50, 10, 500),
    # Subtitle search (OpenSubtitles): key from opensubtitles.com -> Settings -> API
    _spec("sub.api_key", ""),
    _spec("sub.languages", "en"),
    # Input
    _spec("input.wheel", "volume", choices=("volume", "seek", "ignore")),
    _spec("input.click", "play_pause", choices=("play_pause", "none")),
    _spec("input.double_click", "fullscreen", choices=("fullscreen", "none")),
    # Interface
    _spec("ui.overlay_delay_ms", 2600, 600, 12000),
    _spec("ui.always_on_top", False),
    # Files
    _spec("files.resume", "always", choices=("always", "ask", "off")),
    _spec("files.last_dir", ""),
)

SPECS_BY_KEY: dict[str, Spec] = {spec.key: spec for spec in SPECS}
DEFAULTS: dict[str, SettingValue] = {spec.key: spec.default for spec in SPECS}


def _base_dir() -> Path:
    """Folder of the executable when frozen, of the project when run from source."""
    if getattr(sys, "frozen", False):  # pragma: no cover - only in packaged builds
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def is_portable() -> bool:
    return (_base_dir() / PORTABLE_MARKER).exists()


def cache_dir() -> Path:
    """Folder for data that can be rebuilt (loudness maps).

    Portable builds promise that nothing lands outside the app folder, so the
    cache goes next to the exe there; otherwise it belongs in LOCALAPPDATA,
    where a user deleting it costs a re-analysis and nothing else.
    """
    return _base_dir() if is_portable() else local_app_dir()


def settings_path() -> Path:
    r"""Per-user settings file: %APPDATA%\Reel\settings.ini.

    Roaming rather than local, because these are preferences rather than a cache
    and a roaming profile should carry them. The fallback covers a stripped
    environment where APPDATA is not set at all.
    """
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    return root / SETTINGS_DIR / SETTINGS_INI


def _copy_keys(source: QSettings, target: Path) -> None:
    """Copy every key from `source` into the INI at `target`.

    QSettings flattens groups with '/', so a straight key-for-key copy keeps the
    layout. The source is read only: if anything about this goes wrong the old
    values are still where they were, and a missing key just means a default.
    """
    keys = [key for key in source.allKeys() if not key.startswith(_LEGACY_SKIP)]
    if not keys:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    dest = QSettings(str(target), QSettings.Format.IniFormat)
    for key in keys:
        if not dest.contains(key):  # never overwrite a file that already has an opinion
            dest.setValue(key, source.value(key))
    dest.sync()


def _migrate_legacy(target: Path) -> None:
    """Pull 0.0.1's registry settings into the INI, once."""
    try:
        _copy_keys(QSettings(LEGACY_REGISTRY, QSettings.Format.NativeFormat), target)
    except OSError:
        # No permission is not worth failing a launch over: the app starts on
        # defaults and the next save writes the file if it can.
        pass


def _coerce(spec: Spec, raw: object) -> SettingValue:
    """Turn whatever a hand-edited store contains into the declared type."""
    default = spec.default

    if isinstance(default, bool):
        # INI stores "true"/"false"; bool("false") would be True, so be explicit.
        if isinstance(raw, str):
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        return bool(raw)

    if isinstance(default, int):
        try:
            value = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default
        if spec.minimum is not None:
            value = max(int(spec.minimum), value)
        if spec.maximum is not None:
            value = min(int(spec.maximum), value)
        return value

    if isinstance(default, (list, tuple)):
        # Key bindings: a list of key-sequence strings. INI hands back a
        # comma-separated string, the registry a QStringList; accept both.
        if isinstance(raw, str):
            items = raw.split(",")
        elif raw is None:
            items = []
        else:
            try:
                items = [str(item) for item in raw]  # type: ignore[union-attr]
            except TypeError:
                return list(default)
        cleaned: list[str] = []
        for item in items:
            text = item.strip()
            if text and text not in cleaned:
                cleaned.append(text)
        return cleaned

    if raw is None:
        return str(default)
    value = str(raw)
    if spec.choices is not None and value not in spec.choices:
        return str(default)  # a hand-edited unknown mode falls back, never crashes
    return value


class Settings(QObject):
    """Typed, debounced access to the preference store."""

    changed = Signal(str)  # key

    def __init__(self, path: str | Path | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)

        self._store = self._open_store(path)
        # Instance copy: other modules (shortcuts) register their own specs, so
        # the key list is owned where the defaults live, not here.
        self._specs: dict[str, Spec] = dict(SPECS_BY_KEY)
        self._values: dict[str, SettingValue] = {
            spec.key: _coerce(spec, self._store.value(spec.key, spec.default)) for spec in SPECS
        }
        self._dirty = False

        # Volume arrives in bursts (wheel, held key, slider drag). Writing the
        # file on every tick is wasteful, so the flush is debounced.
        self._save = QTimer(self)
        self._save.setSingleShot(True)
        self._save.setInterval(SAVE_DEBOUNCE_MS)
        self._save.timeout.connect(self.flush)

    # ------------------------------------------------------------------ store
    def _open_store(self, path: str | Path | None) -> QSettings:
        if path is not None:
            return QSettings(str(path), QSettings.Format.IniFormat)
        if is_portable():
            return QSettings(str(_base_dir() / PORTABLE_INI), QSettings.Format.IniFormat)
        target = settings_path()
        if not target.exists():
            _migrate_legacy(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        return QSettings(str(target), QSettings.Format.IniFormat)

    def location(self) -> str:
        """Where values actually live - shown in the settings UI later."""
        return self._store.fileName()

    def has(self, key: str) -> bool:
        """True if the store already holds this key (used for first-run detection)."""
        return self._store.contains(key)

    def add_specs(self, specs: Iterable[Spec]) -> None:
        """Register specs owned by another module, e.g. one per key binding.

        Called before the values are read, so a module can keep its defaults next
        to the thing it defines instead of in this file.
        """
        for spec in specs:
            if spec.key in self._specs:
                continue
            self._specs[spec.key] = spec
            self._values[spec.key] = _coerce(spec, self._store.value(spec.key, spec.default))

    def default(self, key: str) -> SettingValue:
        spec = self._specs.get(key)
        if spec is None:  # pragma: no cover - programming error, fail loud
            raise KeyError(f"unknown setting {key!r}")
        default = spec.default
        return list(default) if isinstance(default, (list, tuple)) else default

    def specs(self) -> dict[str, Spec]:
        return dict(self._specs)

    # ----------------------------------------------------------------- values
    def get(self, key: str) -> SettingValue:
        try:
            return self._values[key]
        except KeyError:  # pragma: no cover - programming error, fail loud
            raise KeyError(f"unknown setting {key!r}") from None

    def set(self, key: str, value: object) -> None:
        spec = self._specs.get(key)
        if spec is None:  # pragma: no cover - programming error, fail loud
            raise KeyError(f"unknown setting {key!r}")

        cleaned = _coerce(spec, value)
        if self._values.get(key) == cleaned:
            return  # no signal for no-ops: this is what stops feedback loops

        self._values[key] = cleaned
        self._store.setValue(key, cleaned)
        self._dirty = True
        self._save.start()
        self.changed.emit(key)

    def reset(self, key: str | None = None) -> None:
        """Restore one key (or everything) to its default."""
        keys = [key] if key is not None else list(self._specs)
        for name in keys:
            self.set(name, self.default(name))

    def flush(self) -> None:
        """Write pending changes now. Called on close so nothing is lost."""
        self._save.stop()
        if self._dirty:
            self._dirty = False
            self._store.sync()

    def as_dict(self) -> dict[str, SettingValue]:
        return dict(self._values)




# ---------------------------------------------------------------- resume store
# Per-file playback positions cannot live in SPECS: the keys are unbounded and
# differ per user. This class is the one deliberate exception to "nobody else
# touches QSettings" - it sits in this module and goes through the same store,
# so there is still exactly one owner of the backend.
RESUME_MIN_MS = 5_000  # shorter than this is not worth remembering
RESUME_TAIL_MS = 10_000  # near the end = finished, so start from the beginning
RESUME_LIMIT = 200  # keeps a long-lived install from growing forever

_RESUME_PREFIX = "resume/"


class ResumeStore:
    """Remembers "where was I" per file, keyed by a hash of the absolute path."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _key(self, path: str) -> str:
        normalised = os.path.normcase(os.path.abspath(path))
        digest = hashlib.sha1(normalised.encode("utf-8")).hexdigest()
        return f"{_RESUME_PREFIX}{digest}"

    def position(self, path: str) -> int:
        if not path:
            return 0
        try:
            return max(0, int(self._settings._store.value(self._key(path), 0)))
        except (TypeError, ValueError):
            return 0

    def remember(self, path: str, position_ms: int, duration_ms: int) -> None:
        if not path or duration_ms <= 0:
            return
        if position_ms < RESUME_MIN_MS or position_ms > duration_ms - RESUME_TAIL_MS:
            self.forget(path)
            return
        self._settings._store.setValue(self._key(path), int(position_ms))
        self._trim()

    def forget(self, path: str) -> None:
        if path:
            self._settings._store.remove(self._key(path))

    # Per-file *choices*, separate from resume positions: which subtitle track
    # the user picked for this film, so reopening does not fall back to VLC's
    # default (often "the one that happens to be first").
    def pick(self, path: str, slot: str) -> str:
        if not path:
            return ""
        return str(self._settings._store.value(f"{self._key(path)}/{slot}", ""))

    def remember_pick(self, path: str, slot: str, value: str) -> None:
        if not path:
            return
        self._settings._store.setValue(f"{self._key(path)}/{slot}", str(value))

    def _trim(self) -> None:
        store = self._settings._store
        keys = [key for key in store.allKeys() if key.startswith(_RESUME_PREFIX)]
        for key in keys[:-RESUME_LIMIT] if len(keys) > RESUME_LIMIT else []:
            store.remove(key)

    def flush(self) -> None:
        self._settings.flush()


__all__ = [
    "DEFAULTS",
    "PORTABLE_MARKER",
    "ResumeStore",
    "SettingValue",
    "Spec",
    "SPECS",
    "SPECS_BY_KEY",
    "Settings",
    "cache_dir",
    "is_portable",
    "settings_path",
]
