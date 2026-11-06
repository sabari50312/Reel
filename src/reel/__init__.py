"""Reel — a low-latency desktop media player.

The UI never imports libvlc; everything that touches libVLC lives in
reel.engine and surfaces as plain Qt signals.
"""

__version__ = "0.0.1"

__all__ = ["__version__"]
