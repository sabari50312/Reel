"""Draw Reel's app icon and pack it into a multi-size .ico.

The mark is painter geometry, not an image: the same reason the ui icons in
`reel/widgets/icons.py` are paths. Drawn at each size instead of downscaled from
one big bitmap, so the 16px version is designed rather than a shrunk 256.

  python tools/make_icon.py            # writes reel/assets/icon/Reel.ico + .png
  python tools/make_icon.py --preview  # also dumps an 8x strip to %TEMP%

Two sizes of the mark exist, because one drawing cannot serve both ends:
  * 24px and below: no seam between the frames (it would be sub-pixel anyway),
    a slightly larger mark and a bigger play triangle
  * 32px and above: the seam that makes the second frame read as *behind* the
    first one
"""

from __future__ import annotations

import argparse
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QBuffer, QIODevice, QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor,
    QGuiApplication,
    QImage,
    QPainter,
    QPainterPath,
    QPolygonF,
    QPen,
)

from reel.style import tokens  # noqa: E402

ICON_DIR = ROOT / "src" / "reel" / "assets" / "icon"
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

TILE = QColor(tokens.ACCENT_GREEN)
INK = QColor(tokens.BG)


def _scaled(rect: QRectF, factor: float) -> QRectF:
    """Scale a unit-space rect about the icon centre."""
    centre = QPointF(0.5, 0.5)

    def move(value: float) -> float:
        return 0.5 + (value - 0.5) * factor

    return QRectF(
        move(rect.x()),
        move(rect.y()),
        rect.width() * factor,
        rect.height() * factor,
    )


def _rounded(rect: QRectF) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(rect, rect.width() * 0.17, rect.width() * 0.17)
    return path


def _frame(rect: QRectF, radius: float) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path


def _stroke(painter: QPainter, path: QPainterPath, colour: QColor, width: float) -> None:
    pen = QPen(colour)
    pen.setWidthF(width)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(path)


def render(size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    # Everything below is written in a unit square and scaled here, so the same
    # geometry reads at 16px as at 256px.
    painter.scale(float(size), float(size))

    painter.setBrush(TILE)
    painter.drawRoundedRect(QRectF(0.0, 0.0, 1.0, 1.0), 0.18, 0.18)

    small = size <= 24
    if small:
        # A second frame at this resolution is a smudge, so the icon is one cell
        # with a bigger play glyph and a heavier outline.
        cell = QRectF(0.20, 0.255, 0.60, 0.49)
        _stroke(painter, _frame(cell, 0.09), INK, 0.075)
        height = 0.235
    else:
        front = QRectF(0.130, 0.180, 0.405, 0.320)
        back = QRectF(0.440, 0.490, 0.370, 0.295)
        weight = 0.050
        gap = 0.028
        back_path = _frame(back, 0.062)
        front_path = _frame(front, 0.062)
        _stroke(painter, back_path, INK, weight)
        # A seam of tile colour, then the frame itself: the gap reads as the
        # front cell sitting in front of the back one rather than fused to it.
        _stroke(painter, front_path, TILE, weight + gap * 2.0)
        _stroke(painter, front_path, INK, weight)
        cell = front
        height = 0.150

    centre = cell.center()
    width = height * 0.82
    left = centre.x() - width * 0.40
    top = centre.y() - height / 2.0
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(INK)
    painter.drawPolygon(
        QPolygonF(
            [
                QPointF(left, top),
                QPointF(left, top + height),
                QPointF(left + width, top + height / 2.0),
            ]
        )
    )
    painter.end()
    return image


def png_bytes(image: QImage) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    data = bytes(buffer.data())
    buffer.close()
    return data


def write_ico(path: Path, images: list[QImage]) -> None:
    """A 32-bit icon: header, one 16-byte directory entry per size, PNG payloads.

    Windows has accepted PNG-compressed entries since Vista; the 256px one *must*
    be PNG, and mixing is allowed, so the whole file is built that way.
    """
    blobs = [png_bytes(image) for image in images]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    payload = b""
    for image, blob in zip(images, blobs):
        width = 0 if image.width() >= 256 else image.width()
        height = 0 if image.height() >= 256 else image.height()
        entries += struct.pack(
            "<BBBBHHII", width, height, 0, 0, 1, 32, len(blob), offset + len(payload)
        )
        payload += blob
    path.write_bytes(header + entries + payload)


def options() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--preview", action="store_true", help="write a zoomed size strip to %TEMP%"
    )
    return parser.parse_args()


def main() -> int:
    args = options()
    # Only the preview strip needs a font database, but creating the application
    # unconditionally keeps the two paths identical.
    QGuiApplication.instance() or QGuiApplication([])
    ICON_DIR.mkdir(parents=True, exist_ok=True)

    images = [render(size) for size in SIZES]
    ico = ICON_DIR / "Reel.ico"
    write_ico(ico, images)
    print(f"ico: {ico} ({ico.stat().st_size} bytes, {len(images)} sizes)")

    big = render(512)
    png = ICON_DIR / "Reel.png"
    big.save(str(png), "PNG")
    print(f"png: {png} ({png.stat().st_size} bytes)")

    if args.preview:
        strip = QImage(1400, 340, QImage.Format.Format_ARGB32_Premultiplied)
        strip.fill(QColor("#5c6470"))
        painter = QPainter(strip)
        x = 12
        for image in images:
            zoom = max(1, min(8, 300 // image.width()))
            scaled = image.scaled(
                image.width() * zoom,
                image.height() * zoom,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
            painter.drawImage(x, 20, scaled)
            painter.setPen(QColor("#0b0d10"))
            # no font database beyond the app's own bundled faces here, so label
            # in digits only
            painter.drawText(x, 20 + scaled.height() + 22, str(image.width()))
            x += scaled.width() + 26
        painter.end()
        out = Path(tempfile.gettempdir()) / "reel_icon_preview.png"
        strip.save(str(out), "PNG")
        print(f"preview: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
