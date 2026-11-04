"""Windows caption theming — dark native title bar.

M1 keeps the *native* frame (a custom frameless title bar is explicitly out of
scope). Windows draws that frame light by default, so we ask DWM to paint it
dark and to use Reel's own caption/text colors.

All of this is best-effort: missing attributes on older builds simply fail with
an HRESULT and are ignored, leaving the default (light) frame in place.
"""

from __future__ import annotations

import ctypes
import sys

# DWMWINDOWATTRIBUTE values (dwmapi.h)
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20  # Win10 20H1+ / Win11
_DWMWA_USE_IMMERSIVE_DARK_MODE_PRE_20H1 = 19  # Win10 1809-1909
_DWMWA_CAPTION_COLOR = 35  # Win11 22000+
_DWMWA_TEXT_COLOR = 36  # Win11 22000+
_DWMWA_BORDER_COLOR = 34  # Win11 22000+

_S_OK = 0


def _colorref(qt_hex: str) -> ctypes.c_uint:
    """#RRGGBB -> Win32 COLORREF (0x00BBGGRR)."""
    value = qt_hex.lstrip("#")
    r = int(value[0:2], 16)
    g = int(value[2:4], 16)
    b = int(value[4:6], 16)
    return ctypes.c_uint(r | (g << 8) | (b << 16))


def _set_attr(hwnd: int, attr: int, value: ctypes._SimpleCData) -> bool:  # type: ignore[attr-defined]
    try:
        result = ctypes.windll.dwmapi.DwmSetWindowAttribute(  # type: ignore[attr-defined]
            ctypes.c_void_p(hwnd),
            ctypes.c_uint(attr),
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
    except Exception:  # noqa: BLE001 - theming must never break startup
        return False
    return result == _S_OK


def apply_dark_titlebar(widget, caption: str, text: str, border: str | None = None) -> bool:
    """Paint `widget`'s native caption dark. Returns True if the dark flag took effect.

    Must be called after the window handle exists (e.g. from showEvent or after
    an explicit winId() call).
    """
    if sys.platform != "win32":
        return False

    try:
        hwnd = int(widget.winId())
    except Exception:  # noqa: BLE001
        return False
    if not hwnd:
        return False

    dark = ctypes.c_int(1)
    ok = _set_attr(hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE, dark)
    if not ok:
        ok = _set_attr(hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE_PRE_20H1, dark)

    # Win11: match the caption to the UI instead of the near-black default.
    _set_attr(hwnd, _DWMWA_CAPTION_COLOR, _colorref(caption))
    _set_attr(hwnd, _DWMWA_TEXT_COLOR, _colorref(text))
    if border is not None:
        _set_attr(hwnd, _DWMWA_BORDER_COLOR, _colorref(border))
    return ok


# Keep the player above other windows without touching Qt's window flags.
# setWindowFlag(WindowStaysOnTopHint) re-creates the native window, which would
# destroy the HWND libVLC renders into; SetWindowPos just changes the z-order.
_HWND_TOPMOST = ctypes.c_void_p(-1)
_HWND_NOTOPMOST = ctypes.c_void_p(-2)
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010


def set_topmost(widget, enabled: bool) -> bool:
    """Pin `widget` above other windows (or release it). Returns True on success."""
    if sys.platform != "win32":
        return False
    try:
        hwnd = int(widget.winId())
    except Exception:  # noqa: BLE001
        return False
    if not hwnd:
        return False
    insert_after = _HWND_TOPMOST if enabled else _HWND_NOTOPMOST
    try:
        ok = ctypes.windll.user32.SetWindowPos(  # type: ignore[attr-defined]
            ctypes.c_void_p(hwnd),
            insert_after,
            0,
            0,
            0,
            0,
            _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE,
        )
    except Exception:  # noqa: BLE001
        return False
    return bool(ok)
