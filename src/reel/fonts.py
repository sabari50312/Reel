"""Bundled typefaces: the wordmark face and the UI face.

Reel ships its own fonts because a media player cannot assume the machine has a
characterful face installed - and the wordmark has to be identical everywhere, or
the logo changes per machine. The UI face is bundled for a plainer reason: Segoe
UI is the Windows default (instantly recognisable), and the previous pick,
Schibsted Grotesk, turned out to be too tight-apertured at 11px, where the app
lives.

Two faces, one loader:
  * Sora SemiBold  -> `tokens.FONT_DISPLAY`, the wordmark on the home screen
  * Inter         -> `tokens.FONT_FAMILY`, everything else (400/500/600 - the three
                     weights tokens.qss asks for, so none of it is synthesised).
                     Instanced from the variable original at opsz=14, Inter's own
                     text optical size, rather than its 32px display cut.

Timecodes and the sound map keep `tokens.FONT_MONO` (Consolas) so the digits stay
tabular and the clock does not jitter as it ticks.

Sitting inside the package (`reel/assets/fonts/`) rather than in a top-level
`assets/` folder means a packaged build picks it up with the module tree.
"""

from __future__ import annotations

import sys
from pathlib import Path

FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"


def register_fonts() -> list[str]:
    """Load every bundled font into Qt and return the family names that landed.

    Must run after the QApplication exists (Qt has no font database before that).
    A missing or corrupt file is logged and skipped: a font is never worth an
    exception on startup.
    """
    from PySide6.QtGui import QFontDatabase

    families: list[str] = []
    if not FONT_DIR.is_dir():
        return families
    for path in sorted(FONT_DIR.glob("*.ttf")):
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id < 0:
            print(f"Reel: could not load font {path.name}", file=sys.stderr)
            continue
        families.extend(QFontDatabase.applicationFontFamilies(font_id))
    return families


__all__ = ["FONT_DIR", "register_fonts"]
