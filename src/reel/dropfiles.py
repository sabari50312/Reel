"""Dropping a file on the window, the shell way.

Why this is not just `setAcceptDrops(True)`: libVLC's vout creates its own child
window inside our surface HWND and that child covers the video area, so most of
the time the drop lands on a window whose procedure belongs to VLC, where Qt's
handler never runs. `DragAcceptFiles` + `WM_DROPFILES` on our own surface HWND
covers both cases in practice (verified by hand: dropping on the picture while a
video plays does open the new file), and `WM_DROPFILES` arrives on Qt's
application-level dispatcher, which `QAbstractNativeEventFilter` sees.

Qt's own `setAcceptDrops` still handles the frame (title bar, resize borders).
If a future Windows build stops forwarding these over VLC's child, the fallback
is an OLE IDropTarget registered with `RegisterDragDrop` on that child HWND.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Callable

from PySide6.QtCore import QAbstractNativeEventFilter

WM_DROPFILES = 0x0233

_shell32 = ctypes.windll.shell32
_shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
_shell32.DragQueryFileW.restype = wintypes.UINT
_shell32.DragFinish.argtypes = [wintypes.HANDLE]
_shell32.DragFinish.restype = None
_shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
_shell32.DragAcceptFiles.restype = None


def accept_files(hwnd: int, enabled: bool = True) -> None:
    """Ask the shell to send WM_DROPFILES to this window (shell32, not user32)."""
    if hwnd:
        _shell32.DragAcceptFiles(wintypes.HWND(hwnd), wintypes.BOOL(enabled))


def dropped_paths(hdrop: int) -> list[str]:
    """Read every path out of an HDROP and release it."""
    paths: list[str] = []
    try:
        count = _shell32.DragQueryFileW(wintypes.HANDLE(hdrop), 0xFFFFFFFF, None, 0)
        for index in range(count):
            length = _shell32.DragQueryFileW(wintypes.HANDLE(hdrop), index, None, 0)
            buffer = ctypes.create_unicode_buffer(length + 1)
            if _shell32.DragQueryFileW(wintypes.HANDLE(hdrop), index, buffer, length + 1):
                paths.append(buffer.value)
    finally:
        _shell32.DragFinish(wintypes.HANDLE(hdrop))
    return paths


class DropFileFilter(QAbstractNativeEventFilter):
    """Turn WM_DROPFILES into Python paths, on the GUI thread."""

    def __init__(self, on_files: Callable[[list[str]], None]) -> None:
        super().__init__()
        self._on_files = on_files

    def nativeEventFilter(self, event_type, message):  # noqa: ANN001 - Qt signature
        if event_type in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_DROPFILES:
                paths = dropped_paths(int(msg.wParam))
                if paths:
                    self._on_files(paths)
                return True, 0  # consumed: the default handler would ignore it anyway
        return False, 0


__all__ = ["accept_files", "dropped_paths", "DropFileFilter", "WM_DROPFILES"]
