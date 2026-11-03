"""Stylesheet loading: render tokens.qss with the values from tokens.py."""

from __future__ import annotations

from pathlib import Path
from string import Template

from . import tokens

_QSS_PATH = Path(__file__).with_name("tokens.qss")


def stylesheet() -> str:
    """Compiled QSS with every $token replaced. Cheap enough to call once at startup."""
    template = Template(_QSS_PATH.read_text(encoding="utf-8"))
    return template.safe_substitute(tokens.QSS_VARS)


__all__ = ["stylesheet", "tokens"]
