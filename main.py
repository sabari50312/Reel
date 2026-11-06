"""Reel launcher.

Three jobs and nothing else:
  1. find libVLC and put its folder on the DLL search path *before* anything
     imports vlc (python-vlc does not locate libvlc.dll on its own),
  2. make sure only one player runs, forwarding a file to it when it does,
  3. start the Qt application with Reel's stylesheet.

`vlc_dir()` matters for packaging: a built Reel carries its own `vlc/` folder
next to the exe, so the search order is environment override, bundled copy,
project-local copy, then the machine's VLC install.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
INSTALLED_VLC = Path(r"C:\Program Files\VideoLAN\VLC")
PROJECT_ROOT = Path(__file__).resolve().parent


def vlc_dir() -> Path | None:
    """Folder containing libvlc.dll, or None when no copy can be found."""
    exe_dir = Path(sys.executable).resolve().parent
    candidates = []
    override = os.environ.get("REEL_VLC_DIR")
    if override:
        candidates.append(Path(override))
    candidates += [exe_dir / "vlc", exe_dir / "VLC", PROJECT_ROOT / "vlc", INSTALLED_VLC]
    for candidate in candidates:
        if (candidate / "libvlc.dll").is_file():
            return candidate
    return None


def register_libvlc() -> Path | None:
    """Make libvlc.dll importable. Safe to call when VLC is not installed."""
    folder = vlc_dir()
    if folder is None or sys.platform != "win32":
        return folder
    os.add_dll_directory(str(folder))
    plugins = folder / "plugins"
    if plugins.is_dir():
        # libVLC normally looks next to its own DLL, but being explicit survives
        # the copy step of a packaged build.
        os.environ.setdefault("VLC_PLUGIN_PATH", str(plugins))
    return folder


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    register_libvlc()

    from PySide6.QtWidgets import QApplication

    from reel.fonts import register_fonts
    from reel.main_window import MainWindow
    from reel.style import stylesheet

    app = QApplication(argv)
    # QSettings' native backend keys on these: HKCU\Software\Reel\Reel
    app.setOrganizationName("Reel")
    app.setApplicationName("Reel")
    app.setApplicationDisplayName("Reel")
    app.setStyle("Fusion")  # predictable dark chrome for menus/tooltips
    # Before the stylesheet: the bundled faces (Sora for the wordmark, Inter for
    # everything else) have to be in Qt's font database before anything asks for
    # them, or Qt silently substitutes Segoe UI.
    register_fonts()
    app.setStyleSheet(stylesheet())

    from reel.single_instance import SingleInstance

    media = next((arg for arg in argv[1:] if not arg.startswith("-")), None)
    single = SingleInstance()
    if not single.acquire(media or ""):
        return 0  # handed to the running window; nothing left to do

    window = MainWindow(media)

    def _forward(path: str) -> None:
        if path:
            window.open_file(path)
        window.showNormal()
        window.raise_()
        window.activateWindow()

    single.message.connect(_forward)
    window.show()

    # A portable folder that has been moved leaves its "Open with" entry pointing
    # at the old path. The user already opted in when they turned that on, so fix
    # the path silently; nothing is registered for anyone who never did.
    from reel.associations import refresh_if_moved

    refresh_if_moved()

    exit_code = app.exec()
    single.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
