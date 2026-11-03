"""Key bindings, expressed per action and never per key.

An action owns a *tuple* of key sequences, so:
  * one action can have several bindings (Space and K both toggle playback),
  * a key that no action lists simply does not exist (nothing is bound to S -
    there is no S entry anywhere, not a disabled one),
  * the future keybinding editor rewrites `keys` for an action id, which is why
    the GUI code only ever calls `action_*` handlers and never inspects keys.

`ShortcutRouter` is the only place that touches keys, so a future
"bindings from settings" feature swaps the table, not the wiring.

Why an event filter instead of QShortcut: QShortcut ignores the
auto-repeat KeyPress events that Windows sends while a key is held, so holding
Up/Down only moved one step. The router instead ignores just the auto-repeat
ones too, and runs its own repeat timer - which keeps the ramp rate identical
on every keyboard and every Windows repeat setting.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QKeyEvent, QKeySequence
from PySide6.QtWidgets import QApplication, QWidget

# Held keys wait this long, then fire this often. 18 steps/second feels like a
# slider sweep; going faster would outrun libVLC's volume calls and the pill.
HOLD_DELAY_MS = 400
REPEAT_MS = 55

# Only these repeat while held. Seeking is a real demux jump in libVLC (~the
# seek guard window), so hammering it 18x/second would stall the very playback
# the user is scrubbing through.
REPEAT_ACTIONS = frozenset({"volume_up", "volume_down"})


@dataclass(frozen=True)
class Action:
    id: str  # stable id, the thing a keybinding map would key on
    label: str  # human name, for a future settings UI
    keys: tuple[str, ...]  # every binding for this action; empty = unreachable
    method: str  # handler name on the window


ACTIONS: tuple[Action, ...] = (
    Action("play_pause", "Play / Pause", ("Space", "K"), "action_play_pause"),
    Action("seek_back_5", "Back 5 s", ("Left",), "action_seek_back_5"),
    Action("seek_fwd_5", "Forward 5 s", ("Right",), "action_seek_fwd_5"),
    Action("seek_back_30", "Back 30 s", ("Shift+Left",), "action_seek_back_30"),
    Action("seek_fwd_30", "Forward 30 s", ("Shift+Right",), "action_seek_fwd_30"),
    Action("seek_back_10", "Back 10 s", ("J",), "action_seek_back_10"),
    Action("seek_fwd_10", "Forward 10 s", ("L",), "action_seek_fwd_10"),
    Action("volume_up", "Volume up", ("Up",), "action_volume_up"),
    Action("volume_down", "Volume down", ("Down",), "action_volume_down"),
    Action("mute", "Mute", ("M",), "action_mute"),
    Action("fullscreen", "Fullscreen", ("F",), "action_fullscreen"),
    Action("exit_fullscreen", "Exit fullscreen", ("Esc",), "action_exit_fullscreen"),
    Action("open", "Open file", ("Ctrl+O",), "action_open"),
    Action("quit", "Quit", ("Ctrl+Q",), "action_quit"),
)

ACTIONS_BY_ID: dict[str, Action] = {action.id: action for action in ACTIONS}


def _sequence_text(event: QKeyEvent) -> str:
    """Match the table's spelling of a key ("Shift+Left", "Ctrl+O", "Space")."""
    return QKeySequence(event.keyCombination()).toString(
        QKeySequence.SequenceFormat.PortableText
    )


class ShortcutRouter(QObject):
    """Turns key presses on the player window into `action_*` calls."""

    def __init__(self, host: QWidget) -> None:
        super().__init__(host)
        self._host = host
        self._bindings: dict[str, Action] = {}
        for action in ACTIONS:
            if getattr(host, action.method, None) is None:
                # Fail loud at start-up rather than with a dead key later.
                raise AttributeError(f"{type(host).__name__} has no {action.method}()")
            for key in action.keys:
                self._bindings[_sequence_text_of(key)] = action

        self._held: Action | None = None
        self._held_key = 0
        self._delay = QTimer(self)
        self._delay.setSingleShot(True)
        self._delay.setInterval(HOLD_DELAY_MS)
        self._delay.timeout.connect(self._start_repeating)
        self._repeat = QTimer(self)
        self._repeat.setInterval(REPEAT_MS)
        self._repeat.timeout.connect(self._repeat_once)

        # App-level filter, but only events inside this window are handled: the
        # video surface owns focus while it is clicked, and its key events
        # propagate up to the window, where a window-level filter would see them
        # - installing on the app just removes the reliance on that propagation.
        app = QApplication.instance()
        if app is None:  # pragma: no cover - host needs a QApplication anyway
            raise RuntimeError("ShortcutRouter needs a QApplication")
        app.installEventFilter(self)

    # ------------------------------------------------------------------ events
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if isinstance(watched, QWidget):
            if watched.window() is not self._host:
                return False
        elif watched is not QApplication.instance():
            return False

        kind = event.type()
        if kind == QEvent.Type.KeyPress:
            if event.isAutoRepeat():  # our timer owns repetition
                return False
            action = self._bindings.get(_sequence_text(event))
            if action is None:
                return False
            self._fire(action)
            if action.id in REPEAT_ACTIONS:
                self._held = action
                self._held_key = event.key()
                self._repeat.stop()
                self._delay.start()
            return True

        if kind == QEvent.Type.KeyRelease:
            if event.isAutoRepeat():
                return False
            if self._held is not None and event.key() == self._held_key:
                self._stop()
                return True
            return False

        # Losing the key-release event would leave the volume ramping forever.
        if kind in (QEvent.Type.WindowDeactivate, QEvent.Type.FocusOut, QEvent.Type.Hide):
            self._stop()
        return False

    # ------------------------------------------------------------------ repeat
    def _fire(self, action: Action) -> None:
        getattr(self._host, action.method)()

    def _start_repeating(self) -> None:
        if self._held is not None:
            self._repeat.start()

    def _repeat_once(self) -> None:
        if self._held is None:
            self._repeat.stop()
            return
        self._fire(self._held)

    def _stop(self) -> None:
        self._delay.stop()
        self._repeat.stop()
        self._held = None
        self._held_key = 0


def _sequence_text_of(key: str) -> str:
    return QKeySequence(key).toString(QKeySequence.SequenceFormat.PortableText)


def attach(host: QWidget) -> ShortcutRouter:
    """Bind every action to `host`; keep the returned router alive."""
    return ShortcutRouter(host)


__all__ = ["Action", "ACTIONS", "ACTIONS_BY_ID", "ShortcutRouter", "attach"]
