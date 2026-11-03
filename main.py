"""Reel launcher.

Two jobs and nothing else:
  1. put libVLC's directory on the DLL search path *before* anything imports vlc
     (python-vlc does not find libvlc.dll on its own),
  2. start the Qt application with Reel's stylesheet.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
VLC_DIR = Path(r"C:\Program Files\VideoLAN\VLC")


def register_libvlc() -> None:
    """Make libvlc.dll importable. Safe to call when VLC is not installed."""
    if sys.platform == "win32" and VLC_DIR.is_dir():
        os.add_dll_directory(str(VLC_DIR))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    register_libvlc()

    from PySide6.QtWidgets import QApplication

    from reel.main_window import MainWindow
    from reel.style import stylesheet

    app = QApplication(argv)
    app.setApplicationName("Reel")
    app.setApplicationDisplayName("Reel")
    app.setStyle("Fusion")  # predictable dark chrome for menus/tooltips
    app.setStyleSheet(stylesheet())

    media = next((arg for arg in argv[1:] if not arg.startswith("-")), None)
    window = MainWindow(media)
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
