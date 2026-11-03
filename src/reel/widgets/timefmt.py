"""Time formatting shared by the seek bubble and the control bar label."""

from __future__ import annotations


def format_time(ms: int, hours: bool = False) -> str:
    """`5:32` for short media, `1:02:33` when it runs past an hour."""
    total = max(0, int(ms) // 1000)
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    if hours or h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def needs_hours(ms: int) -> bool:
    return ms >= 3_600_000


__all__ = ["format_time", "needs_hours"]
