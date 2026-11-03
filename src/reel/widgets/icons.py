"""Icons as geometry, not images.

Every glyph is built from QPainterPath primitives in a 24x24 box and stroked
with a cosmetic pen, so a single set stays crisp at any DPI and takes its colour
from the current palette (white at rest, dim when disabled). No SVG files, no
QtSvg dependency, no raster scaling.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen

STROKE = 1.8  # px, matches the mockup's SVG stroke width
_BOX = 24.0


# ------------------------------------------------------------------ builders
def _poly(points: list[tuple[float, float]]) -> QPainterPath:
    path = QPainterPath()
    path.moveTo(*points[0])
    for x, y in points[1:]:
        path.lineTo(x, y)
    path.closeSubpath()
    return path


def _line(points: list[tuple[float, float]]) -> QPainterPath:
    path = QPainterPath()
    path.moveTo(*points[0])
    for x, y in points[1:]:
        path.lineTo(x, y)
    return path


def _arc(rect: tuple[float, float, float, float], start: float, span: float) -> QPainterPath:
    path = QPainterPath()
    target = QRectF(*rect)
    path.arcMoveTo(target, start)
    path.arcTo(target, start, span)
    return path


def _rrect(x: float, y: float, w: float, h: float, radius: float) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(QRectF(x, y, w, h), radius, radius)
    return path


_SPEAKER_BODY = _rrect(3.6, 9.4, 3.6, 5.2, 0.6)
_SPEAKER_CONE = _poly([(7.2, 9.4), (11.6, 5.4), (11.6, 18.6), (7.2, 14.6)])

# name -> ((path, filled), ...)
_ICONS: dict[str, tuple[tuple[QPainterPath, bool], ...]] = {
    "play": ((_poly([(9.2, 6.4), (18.6, 12.0), (9.2, 17.6)]), True),),
    "pause": (
        (_rrect(9.0, 6.6, 3.0, 10.8, 0.8), True),
        (_rrect(13.4, 6.6, 3.0, 10.8, 0.8), True),
    ),
    "volume_high": (
        (_SPEAKER_BODY, True),
        (_SPEAKER_CONE, True),
        (_arc((9.2, 7.2, 8.6, 9.6), -58, 116), False),
        (_arc((6.4, 4.4, 14.8, 15.2), -52, 104), False),
    ),
    "volume_low": (
        (_SPEAKER_BODY, True),
        (_SPEAKER_CONE, True),
        (_arc((9.2, 7.2, 8.6, 9.6), -58, 116), False),
    ),
    "volume_mute": (
        (_SPEAKER_BODY, True),
        (_SPEAKER_CONE, True),
        (_line([(14.3, 8.1), (19.9, 15.9)]), False),
    ),
    "open": (
        (
            _line(
                [
                    (3.5, 8.2),
                    (9.0, 8.2),
                    (11.0, 10.6),
                    (20.5, 10.6),
                    (20.5, 18.4),
                    (3.5, 18.4),
                    (3.5, 8.2),
                ]
            ),
            False,
        ),
    ),
    "fullscreen": (
        (_line([(4.0, 9.0), (4.0, 4.0), (9.0, 4.0)]), False),
        (_line([(20.0, 9.0), (20.0, 4.0), (15.0, 4.0)]), False),
        (_line([(4.0, 15.0), (4.0, 20.0), (9.0, 20.0)]), False),
        (_line([(20.0, 15.0), (20.0, 20.0), (15.0, 20.0)]), False),
    ),
    "exit_fullscreen": (
        (_line([(9.0, 4.0), (9.0, 9.0), (4.0, 9.0)]), False),
        (_line([(15.0, 4.0), (15.0, 9.0), (20.0, 9.0)]), False),
        (_line([(9.0, 20.0), (9.0, 15.0), (4.0, 15.0)]), False),
        (_line([(15.0, 20.0), (15.0, 15.0), (20.0, 15.0)]), False),
    ),
}


def paint_icon(
    painter: QPainter,
    name: str,
    rect: QRectF,
    color: str,
    stroke: float = STROKE,
) -> None:
    """Draw `name` centred in `rect`, scaled from the 24x24 design box."""
    shapes = _ICONS.get(name)
    if not shapes or rect.isEmpty():
        return

    side = min(rect.width(), rect.height())
    scale = side / _BOX

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.translate(rect.center())
    painter.scale(scale, scale)
    painter.translate(-_BOX / 2.0, -_BOX / 2.0)

    pen = QPen(QColor(color), stroke)
    pen.setCosmetic(True)  # constant device-pixel width regardless of the scale
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)

    for path, filled in shapes:
        painter.setBrush(QColor(color) if filled else Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

    painter.restore()


__all__ = ["paint_icon", "STROKE"]
