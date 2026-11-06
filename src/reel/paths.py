"""Where Reel's bundled data files live.

One module, so a packaged build has a single place to point at. Everything is
resolved relative to this package rather than the working directory, which is
what makes the Nuitka data-dir mapping (`src/reel/assets` -> `reel/assets`) work
without a single conditional in the app.
"""

from __future__ import annotations

from pathlib import Path

ASSET_DIR = Path(__file__).resolve().parent / "assets"
FONT_DIR = ASSET_DIR / "fonts"
# The .ico doubles as the exe's icon resource (see tools/build.py) and the
# window icon Qt gives the taskbar, alt-tab and every dialog.
ICON = ASSET_DIR / "icon" / "Reel.ico"
