"""Icons as geometry, not images.

Every glyph is built from QPainterPath primitives inside a 24x24 box with a
common optical margin, so the set reads as one family. Two rules matter for how
these look at 24px:

  * **Corners are explicit, not implied.** A filled polygon keeps razor tips
    that look like rendering dirt. `_rounded_poly` replaces each vertex with a
    quadratic corner of a chosen radius, and the bars are rounded rects - no
    "stroke the fill to soften it" trick, which silently ate the gap between the
    pause bars.
  * **One stroke width, one optical size.** 2px round-capped strokes, content
    kept inside 3.5..20.5 so no glyph looks larger than its neighbour.

Pens are deliberately not cosmetic: a non-cosmetic pen scales with the painter
transform, so at 125% Windows scaling a 2px stroke becomes 2.5 device pixels
instead of snapping unevenly between 2 and 3.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen

STROKE = 2.0  # design units; scales with the icon size and the device ratio
_BOX = 24.0


# ------------------------------------------------------------------ builders
def _poly(points: list[tuple[float, float]]) -> QPainterPath:
    path = QPainterPath()
    path.moveTo(*points[0])
    for x, y in points[1:]:
        path.lineTo(x, y)
    path.closeSubpath()
    return path


def _corner(
    previous: tuple[float, float],
    vertex: tuple[float, float],
    following: tuple[float, float],
    radius: float,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Where an edge leaves and rejoins a vertex that is being rounded."""

    def along(target: tuple[float, float]) -> tuple[float, float]:
        dx, dy = target[0] - vertex[0], target[1] - vertex[1]
        length = math.hypot(dx, dy) or 1.0
        step = min(radius, length / 2.0) / length
        return (vertex[0] + dx * step, vertex[1] + dy * step)

    return along(previous), along(following)


def _rounded_poly(points: list[tuple[float, float]], radius: float) -> QPainterPath:
    """Closed polygon whose corners are quadratic curves of `radius`."""
    count = len(points)
    entry, exit_ = _corner(points[-1], points[0], points[1], radius)
    path = QPainterPath()
    path.moveTo(*entry)
    for index in range(count):
        previous = points[index - 1]
        vertex = points[index]
        following = points[(index + 1) % count]
        entry, exit_ = _corner(previous, vertex, following, radius)
        path.lineTo(*entry)
        path.quadTo(vertex[0], vertex[1], exit_[0], exit_[1])
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


# One speaker silhouette: body and cone as a single filled shape, like the flat
# speaker icons in Windows 11 and Material - a body plus a separate triangle
# reads as two objects at this size.
_SPEAKER = _rounded_poly(
    [(4.0, 9.7), (7.3, 9.7), (11.6, 5.3), (11.6, 18.7), (7.3, 14.3), (4.0, 14.3)],
    1.1,
)
_MUTE_X = (
    _line([(14.4, 8.6), (19.6, 15.4)]),
    _line([(19.6, 8.6), (14.4, 15.4)]),
)

# name -> ((path, filled[, stroke]), ...). A third element overrides the pen width
# for that shape only (the CC bowls need a thinner stroke than the family's 2px).
_ICONS: dict[str, tuple[tuple, ...]] = {
    "play": ((_rounded_poly([(8.9, 5.9), (18.9, 12.0), (8.9, 18.1)], 1.7), True),),
    # 2.8 units of white between the bars: the previous 1.4 closed up entirely
    # once the bars were rounded, which made the icon a single blob.
    "pause": (
        (_rrect(7.6, 5.6, 3.0, 12.8, 1.5), True),
        (_rrect(13.4, 5.6, 3.0, 12.8, 1.5), True),
    ),
    "volume_high": (
        (_SPEAKER, True),
        (_arc((9.6, 7.6, 7.8, 8.8), -55, 110), False),
        (_arc((7.0, 5.0, 13.0, 14.0), -50, 100), False),
    ),
    "volume_low": (
        (_SPEAKER, True),
        (_arc((9.6, 7.6, 7.8, 8.8), -55, 110), False),
    ),
    "volume_mute": ((_SPEAKER, True), (_MUTE_X[0], False), (_MUTE_X[1], False)),
    "open": (
        (
            _line(
                [
                    (3.9, 17.9),
                    (3.9, 6.6),
                    (9.0, 6.6),
                    (11.0, 8.8),
                    (20.1, 8.8),
                    (20.1, 17.9),
                    (3.9, 17.9),
                ]
            ),
            False,
        ),
    ),
    "fullscreen": (
        (_line([(4.2, 9.4), (4.2, 4.2), (9.4, 4.2)]), False),
        (_line([(19.8, 9.4), (19.8, 4.2), (14.6, 4.2)]), False),
        (_line([(4.2, 14.6), (4.2, 19.8), (9.4, 19.8)]), False),
        (_line([(19.8, 14.6), (19.8, 19.8), (14.6, 19.8)]), False),
    ),
    "exit_fullscreen": (
        (_line([(9.4, 4.2), (9.4, 9.4), (4.2, 9.4)]), False),
        (_line([(14.6, 4.2), (14.6, 9.4), (19.8, 9.4)]), False),
        (_line([(9.4, 19.8), (9.4, 14.6), (4.2, 14.6)]), False),
        (_line([(14.6, 19.8), (14.6, 14.6), (19.8, 14.6)]), False),
    ),
    # Vertical bars of unequal height: the universal "sound" glyph, and the one
    # the reference design uses for its audio tab.
    "waveform": (
        (_rrect(3.6, 9.4, 2.4, 5.2, 1.2), True),
        (_rrect(8.0, 5.6, 2.4, 12.8, 1.2), True),
        (_rrect(12.4, 7.8, 2.4, 8.4, 1.2), True),
        (_rrect(16.8, 10.4, 2.4, 3.6, 1.2), True),
    ),
    "video": (
        (_rrect(3.4, 6.0, 17.2, 12.0, 2.4), False),
        (_line([(3.4, 9.4), (20.6, 9.4)]), False),
        (_line([(3.4, 14.6), (20.6, 14.6)]), False),
        (_line([(8.0, 6.0), (8.0, 18.0)]), False),
        (_line([(16.0, 6.0), (16.0, 18.0)]), False),
    ),
    "key": (
        (_rrect(3.4, 7.0, 17.2, 10.4, 2.2), False),
        (_rrect(6.4, 10.2, 2.0, 2.0, 0.6), True),
        (_rrect(11.0, 10.2, 2.0, 2.0, 0.6), True),
        (_rrect(15.6, 10.2, 2.0, 2.0, 0.6), True),
        (_rrect(8.0, 13.6, 8.0, 1.8, 0.9), True),
    ),
    "layout": (
        (_rrect(3.4, 5.6, 17.2, 12.8, 2.2), False),
        (_line([(14.6, 5.6), (14.6, 18.4)]), False),
        (_line([(14.6, 9.6), (20.6, 9.6)]), False),
    ),
    # Material "tune": three tracks, each *gapped* around its knob. The old
    # version drew one continuous line behind the knobs, which read as mush at
    # 24px - the gap is what makes it legible as a slider.
    "settings": (
        (_line([(3.8, 6.6), (14.4, 6.6)]), False),
        (_line([(17.6, 6.6), (20.2, 6.6)]), False),
        (_rrect(15.4, 3.8, 2.0, 5.6, 1.0), True),
        (_line([(3.8, 12.0), (6.6, 12.0)]), False),
        (_line([(9.4, 12.0), (20.2, 12.0)]), False),
        (_rrect(7.0, 9.2, 2.0, 5.6, 1.0), True),
        (_line([(3.8, 17.4), (10.8, 17.4)]), False),
        (_line([(13.6, 17.4), (20.2, 17.4)]), False),
        (_rrect(11.2, 14.6, 2.0, 5.6, 1.0), True),
    ),
    # Navigation glyphs for the file browser. Arrows share one shape: the shaft
    # plus a chevron, so back/forward mirror exactly instead of drifting apart.
    "home": (
        (_line([(3.6, 11.0), (12.0, 4.0), (20.4, 11.0)]), False),
        (_line([(5.6, 9.6), (5.6, 20.0), (18.4, 20.0), (18.4, 9.6)]), False),
        (_line([(9.6, 20.0), (9.6, 14.2), (14.4, 14.2), (14.4, 20.0)]), False),
    ),
    "back": (
        (_line([(19.4, 12.0), (5.0, 12.0)]), False),
        (_line([(10.8, 6.2), (5.0, 12.0), (10.8, 17.8)]), False),
    ),
    "forward": (
        (_line([(4.6, 12.0), (19.0, 12.0)]), False),
        (_line([(13.2, 6.2), (19.0, 12.0), (13.2, 17.8)]), False),
    ),
    "chevron_down": ((_line([(7.4, 9.8), (12.0, 14.4), (16.6, 9.8)]), False),),
    # ---- transport / utility glyphs for the single-row bar ----------------
    "prev": (
        (_line([(6.6, 6.4), (6.6, 17.6)]), False),
        (_rounded_poly([(17.6, 6.2), (9.4, 12.0), (17.6, 17.8)], 1.3), True),
    ),
    "next": (
        (_line([(17.4, 6.4), (17.4, 17.6)]), False),
        (_rounded_poly([(6.4, 6.2), (14.6, 12.0), (6.4, 17.8)], 1.3), True),
    ),
    # headphones: clearly "sound", and not a second speaker glyph next to volume
    "audio": (
        (_arc((4.4, 4.2, 15.2, 15.2), 0, 180), False),
        (_rrect(3.6, 12.0, 4.6, 7.0, 2.2), True),
        (_rrect(15.8, 12.0, 4.6, 7.0, 2.2), True),
    ),
    # Material's folder, ported straight from the supplied SVG: outline 2..22 by
    # 4..20, so the tab is a full 8 units wide with a chamfered shoulder - the
    # flat wide tab is what keeps it from reading as a generic bucket at 18px.
    "folder": (
        (
            _rounded_poly(
                [
                    (3.0, 5.0),
                    (9.0, 5.0),
                    (11.0, 7.0),
                    (21.0, 7.0),
                    (21.0, 19.0),
                    (3.0, 19.0),
                ],
                1.0,
            ),
            False,
        ),
    ),
    "file": (
        (_rrect(6.2, 3.6, 11.6, 16.8, 2.4), False),
        (_line([(9.4, 10.0), (14.6, 10.0)]), False),
        (_line([(9.4, 13.6), (14.6, 13.6)]), False),
        (_line([(9.4, 17.0), (12.8, 17.0)]), False),
    ),
    "close": (
        (_line([(7.0, 7.0), (17.0, 17.0)]), False),
        (_line([(17.0, 7.0), (7.0, 17.0)]), False),
    ),
    "up": (
        (_line([(12.0, 18.6), (12.0, 6.2)]), False),
        (_line([(6.8, 11.2), (12.0, 6.0), (17.2, 11.2)]), False),
    ),
    # Closed captioning: two real "C" bowls instead of two floating bars. They
    # are stroked thin (1.7) so the counter of each C stays open at 18px.
    "subtitles": (
        (_rrect(3.4, 5.6, 17.2, 12.8, 2.0), False),
        (_arc((5.9, 9.2, 4.6, 5.6), 50, 260), False, 1.7),
        (_arc((13.5, 9.2, 4.6, 5.6), 50, 260), False, 1.7),
    ),
    "speed": (
        (_arc((4.8, 5.0, 14.4, 14.4), 20, 140), False),
        (_line([(12.0, 12.2), (16.2, 8.2)]), False),
    ),
    "queue": (
        (_rrect(4.2, 6.6, 15.6, 2.2, 1.1), True),
        (_rrect(4.2, 11.0, 15.6, 2.2, 1.1), True),
        (_rrect(4.2, 15.4, 9.6, 2.2, 1.1), True),
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
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)

    brush = QColor(color)
    for shape in shapes:
        path, filled = shape[0], shape[1]
        width = shape[2] if len(shape) > 2 else None
        if filled:
            # No pen: a stroke around a filled shape widens it, which closed the
            # 2.8-unit gap between the pause bars down to 0.8.
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(brush)
        else:
            if width is None:
                painter.setPen(pen)
            else:
                thin = QPen(QColor(color), width)
                thin.setCapStyle(Qt.PenCapStyle.RoundCap)
                thin.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                painter.setPen(thin)
            painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

    painter.restore()


__all__ = ["STROKE", "paint_icon"]
