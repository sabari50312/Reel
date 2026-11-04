"""Subtitle cue timings, for the sync strip.

libVLC 3 gives us track names but never cue times, so the strip can only draw
caption bars for a subtitle *file* we loaded ourselves. Both common formats are
just timestamps in text, so this stays a small parser rather than a dependency.
"""

from __future__ import annotations

import re
from pathlib import Path

_STAMP = re.compile(r"(\d{1,3}):(\d{2}):(\d{2})[,.](\d{1,3})")
_ARROW_CUE = re.compile(r"([0-9:.,]+)\s*-->\s*([0-9:.,]+)")
_ASS_DIALOGUE = re.compile(r"^Dialogue:\s*[^,]*,([^,]*),([^,]*)", re.MULTILINE | re.IGNORECASE)


def _ms(stamp: str) -> int | None:
    match = _STAMP.search(stamp.strip())
    if match is None:
        return None
    hours, minutes, seconds, fraction = match.groups()
    millis = int(fraction.ljust(3, "0")[:3])
    return (int(hours) * 3600 + int(minutes) * 60 + int(seconds)) * 1000 + millis


def parse_cues(path: str | Path) -> list[tuple[int, int]]:
    """Return `(start_ms, end_ms)` for every cue in an SRT/VTT/ASS file."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []

    cues: list[tuple[int, int]] = []
    for match in _ARROW_CUE.finditer(text):  # SRT and WebVTT
        start, end = _ms(match.group(1)), _ms(match.group(2))
        if start is not None and end is not None and end > start:
            cues.append((start, end))

    if not cues:  # ASS / SSA: "Dialogue: 0,0:00:01.00,0:00:02.50,..."
        for match in _ASS_DIALOGUE.finditer(text):
            start, end = _ms(match.group(1)), _ms(match.group(2))
            if start is not None and end is not None and end > start:
                cues.append((start, end))

    return sorted(set(cues))


__all__ = ["parse_cues"]
