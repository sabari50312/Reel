"""Key bindings: one table of actions, no hard-wired keys.

The model is *per action*, never per key:
  * an action owns a list of key sequences, so Space and K can both toggle playback,
  * a key that no action lists simply does not exist (nothing is bound to S),
  * bindings live in the settings store as `keys/<action id>`, so the settings
    window can rewrite them and the router picks the change up live.

`ShortcutRouter` deliberately does not use QShortcut: QShortcut swallows the
auto-repeat KeyPress events Windows sends while a key is held, which is why
holding volume used to move a single step. The router owns repeat itself
(`HOLD_DELAY_MS`, then `REPEAT_MS`) and ignores the OS repeats.

It filters at application level but only accepts events for `host`'s window, so
typing in the settings dialog can never trigger a player action.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QKeyCombination, QObject, Qt, QTimer
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QLineEdit,
    QPlainTextEdit,
    QTextEdit,
    QWidget,
)

from .settings import SettingValue, Settings, Spec

KEYS_PREFIX = "keys/"
HOLD_DELAY_MS = 400
REPEAT_MS = 55

_MODIFIERS = {
    Qt.Key.Key_Shift,
    Qt.Key.Key_Control,
    Qt.Key.Key_Alt,
    Qt.Key.Key_Meta,
    Qt.Key.Key_AltGr,
    Qt.Key.Key_CapsLock,
    Qt.Key.Key_NumLock,
    Qt.Key.Key_ScrollLock,
}

_PORTABLE = QKeySequence.SequenceFormat.PortableText


@dataclass(frozen=True)
class Action:
    id: str  # stable id: the key in the settings store
    label: str  # human name, shown in the settings window
    keys: tuple[str, ...]  # default bindings; empty = unreachable by design
    method: str  # handler name on the main window
    repeat: bool = False  # True: holding the key repeats the action


ACTIONS: tuple[Action, ...] = (
    Action("play_pause", "Play / pause", ("Space", "K"), "action_play_pause"),
    Action("seek_back_5", "Back 5 s", ("Left",), "action_seek_back_5"),
    Action("seek_fwd_5", "Forward 5 s", ("Right",), "action_seek_fwd_5"),
    Action("seek_back_30", "Back 30 s", ("Shift+Left",), "action_seek_back_30"),
    Action("seek_fwd_30", "Forward 30 s", ("Shift+Right",), "action_seek_fwd_30"),
    Action("seek_back_10", "Back 10 s", ("J",), "action_seek_back_10"),
    Action("seek_fwd_10", "Forward 10 s", ("L",), "action_seek_fwd_10"),
    Action("subtitle_later", "Subtitle delay later", ("Z",), "action_subtitle_later", repeat=True),
    Action("subtitle_earlier", "Subtitle delay earlier", ("X",), "action_subtitle_earlier", repeat=True),
    Action("subtitle_delay_reset", "Reset subtitle delay", ("Shift+Z",), "action_subtitle_delay_reset"),
    Action("audio_later", "Audio delay later", (".",), "action_audio_later", repeat=True),
    Action("audio_earlier", "Audio delay earlier", (",",), "action_audio_earlier", repeat=True),
    Action("audio_delay_reset", "Reset audio delay", ("Shift+.",), "action_audio_delay_reset"),
    Action("audio_next", "Next audio track", ("B",), "action_audio_next"),
    Action("subtitle_next", "Next subtitle track", ("V",), "action_subtitle_next"),
    Action("subtitle_search", "Search subtitles online", ("Ctrl+Shift+S",), "action_subtitle_search"),
    Action("speed_up", "Speed up", ("]",), "action_speed_up", repeat=True),
    Action("speed_down", "Speed down", ("[",), "action_speed_down", repeat=True),
    Action("speed_normal", "Normal speed", ("=",), "action_speed_normal"),
    Action("volume_up", "Volume up", ("Up",), "action_volume_up", repeat=True),
    Action("volume_down", "Volume down", ("Down",), "action_volume_down", repeat=True),
    Action("mute", "Mute", ("M",), "action_mute"),
    Action("fullscreen", "Fullscreen", ("F",), "action_fullscreen"),
    Action("exit_fullscreen", "Exit fullscreen", ("Esc",), "action_exit_fullscreen"),
    Action("open", "Open file", ("Ctrl+O",), "action_open"),
    Action("settings", "Settings", ("Ctrl+P",), "action_settings"),
    Action("search_settings", "Search settings", ("Ctrl+F",), "action_search_settings"),
    Action("quit", "Quit", ("Ctrl+Q",), "action_quit"),
)

ACTIONS_BY_ID: dict[str, Action] = {action.id: action for action in ACTIONS}


# ------------------------------------------------------------------- sequences
def normalize(sequence: str) -> str | None:
    """Canonical text for a key sequence, or None if it is not a usable binding."""
    text = QKeySequence(sequence).toString(_PORTABLE)
    if not text or text in {"Ctrl", "Alt", "Shift", "Meta"}:
        return None
    return text


def sequence_of_key(key: int, modifiers: Qt.KeyboardModifier) -> str | None:
    """Canonical text for a key press, e.g. Shift+Left. Modifier-only presses → None."""
    if key in _MODIFIERS:
        return None
    # key() arrives as a plain int from Python-built events, so build the
    # combination explicitly instead of relying on `key | modifiers`.
    text = QKeySequence(QKeyCombination(modifiers, Qt.Key(key))).toString(_PORTABLE)
    return text or None


def binding_specs() -> tuple[Spec, ...]:
    """One store entry per action, defaulting to the table above."""
    return tuple(Spec(KEYS_PREFIX + action.id, list(action.keys)) for action in ACTIONS)


# -------------------------------------------------------------------- bindings
class KeyBindings:
    """Read/write layer over the settings store for the binding table."""

    def __init__(self, settings: Settings) -> None:
        # Idempotent registration: a binding store must be readable even if the
        # router has not been built yet (tests, a future --keys dump).
        settings.add_specs(binding_specs())
        self._settings = settings

    def keys(self, action_id: str) -> list[str]:
        value: SettingValue = self._settings.get(KEYS_PREFIX + action_id)
        return list(value) if isinstance(value, list) else []

    def set_keys(self, action_id: str, keys: Sequence[str]) -> None:
        cleaned: list[str] = []
        for item in keys:
            text = normalize(item)
            if text is not None and text not in cleaned:
                cleaned.append(text)
        self._settings.set(KEYS_PREFIX + action_id, cleaned)

    def is_default(self, action_id: str) -> bool:
        default = ACTIONS_BY_ID[action_id].keys
        return self.keys(action_id) == normalize_list(default)

    def owner(self, sequence: str, ignore: str | None = None) -> Action | None:
        """Which action currently holds `sequence` (if any)."""
        text = normalize(sequence)
        if text is None:
            return None
        for action in ACTIONS:
            if action.id != ignore and text in self.keys(action.id):
                return action
        return None

    def assign(self, action_id: str, index: int | None, sequence: str) -> Action | None:
        """Bind `sequence` to `action_id`.

        Returns the action it was taken away from, if it was already in use - the
        UI reports that instead of silently stealing a key or refusing to bind.
        """
        text = normalize(sequence)
        if text is None:
            return None
        stolen_from = self.owner(text, ignore=action_id)
        if stolen_from is not None:
            self.set_keys(stolen_from.id, [k for k in self.keys(stolen_from.id) if k != text])

        keys = self.keys(action_id)
        if index is None or not 0 <= index < len(keys):
            keys.append(text)
        else:
            keys[index] = text
        self.set_keys(action_id, keys)
        return stolen_from

    def remove(self, action_id: str, index: int) -> None:
        keys = self.keys(action_id)
        if 0 <= index < len(keys):
            del keys[index]
            self.set_keys(action_id, keys)

    def reset(self, action_id: str | None = None) -> None:
        ids = [action_id] if action_id else list(ACTIONS_BY_ID)
        for aid in ids:
            self.set_keys(aid, ACTIONS_BY_ID[aid].keys)


def normalize_list(keys: Sequence[str]) -> list[str]:
    out: list[str] = []
    for item in keys:
        text = normalize(item)
        if text is not None and text not in out:
            out.append(text)
    return out


# --------------------------------------------------------------------- router
class ShortcutRouter(QObject):
    """Turns key presses into `action_*` calls on the host window."""

    def __init__(self, host: QWidget, settings: Settings) -> None:
        super().__init__(host)
        self._host = host
        self._bindings = KeyBindings(settings)
        self._handlers: dict[str, object] = {}
        for action in ACTIONS:
            handler = getattr(host, action.method, None)
            if handler is None:  # pragma: no cover - wiring mistake, fail loud early
                raise AttributeError(f"{type(host).__name__} has no {action.method}()")
            self._handlers[action.id] = handler

        self._lookup: dict[str, Action] = {}
        self._held: Action | None = None
        self._held_seq = ""
        self._held_key = 0

        self._delay = QTimer(self)
        self._delay.setSingleShot(True)
        self._delay.setInterval(HOLD_DELAY_MS)
        self._delay.timeout.connect(self._begin_repeat)

        self._repeat = QTimer(self)
        self._repeat.setInterval(REPEAT_MS)
        self._repeat.timeout.connect(self._repeat_once)

        app = QApplication.instance()
        if app is not None:  # pragma: no cover - always true in the real app
            app.installEventFilter(self)
        settings.changed.connect(self._on_setting_changed)
        self.rebuild()

    # -------------------------------------------------------------- bindings
    def bindings(self) -> KeyBindings:
        return self._bindings

    def rebuild(self) -> None:
        lookup: dict[str, Action] = {}
        for action in ACTIONS:
            for text in self._bindings.keys(action.id):
                lookup.setdefault(text, action)  # first action wins on a duplicate
        self._lookup = lookup

    def _on_setting_changed(self, key: str) -> None:
        if key.startswith(KEYS_PREFIX):
            self.rebuild()

    # ---------------------------------------------------------------- events
    def _accepts(self, watched: QObject) -> bool:
        """Keys work in the player and in its own panels, not in other apps.

        The settings panel and the file drawer are separate top-level windows
        parented to the player, so requiring `window() is host` meant every
        shortcut died the moment you clicked anything inside the panel.
        """
        if not isinstance(watched, QWidget):
            return False
        window = watched.window()
        return window is self._host or window.parentWidget() is self._host

    @staticmethod
    def _typing() -> bool:
        """True while a text field owns the keyboard: `m` there is a letter."""
        focus = QApplication.focusWidget()
        return isinstance(focus, (QLineEdit, QTextEdit, QPlainTextEdit))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if not self._accepts(watched):
            return False
        if (
            event.type() == QEvent.Type.KeyPress
            and not event.modifiers()
            and event.key() != Qt.Key.Key_Escape
            and self._typing()
        ):
            return False  # let the user type; Escape still closes the panel

        kind = event.type()
        if kind == QEvent.Type.KeyPress:
            if event.isAutoRepeat():  # our timers own repetition
                return False
            text = QKeySequence(event.keyCombination()).toString(_PORTABLE)
            action = self._lookup.get(text)
            if action is None:
                return False
            self._handlers[action.id]()
            if action.repeat:
                self._held = action
                self._held_seq = text
                self._held_key = event.key()
                self._repeat.stop()
                self._delay.start()
            return True

        if kind == QEvent.Type.KeyRelease:
            if event.isAutoRepeat():
                return False
            if self._held is not None and event.key() == self._held_key:
                self._stop_repeat()
                return True
            return False

        if kind in (QEvent.Type.WindowDeactivate, QEvent.Type.Hide):
            self._stop_repeat()
        return False

    # ---------------------------------------------------------------- repeat
    def _begin_repeat(self) -> None:
        if self._held is not None:
            self._repeat.start()

    def _repeat_once(self) -> None:
        # Re-check the binding: the settings window may have unbound it mid-hold.
        if self._held is None or self._lookup.get(self._held_seq) is not self._held:
            self._stop_repeat()
            return
        self._handlers[self._held.id]()

    def _stop_repeat(self) -> None:
        self._held = None
        self._held_seq = ""
        self._held_key = 0
        self._delay.stop()
        self._repeat.stop()


def attach(host: QWidget, settings: Settings) -> ShortcutRouter:
    """Register the binding specs and return the live router."""
    settings.add_specs(binding_specs())
    return ShortcutRouter(host, settings)


__all__ = [
    "Action",
    "ACTIONS",
    "ACTIONS_BY_ID",
    "HOLD_DELAY_MS",
    "KEYS_PREFIX",
    "KeyBindings",
    "REPEAT_MS",
    "ShortcutRouter",
    "attach",
    "binding_specs",
    "normalize",
    "sequence_of_key",
]
