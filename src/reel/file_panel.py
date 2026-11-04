"""Side file browser - the drawer that replaces the "open a file" dialog.

Why it is a separate top-level window: the picture is a native child HWND that
Windows composites above Qt's own painting, so a panel inside the main window
would be covered by the video. Same trick as the control strip, positioned over
the right edge of the client area instead of the bottom.

It only browses: `os.scandir` of one folder at a time, folders first, media files
filtered by extension. Clicking a folder descends, activating a file emits
`file_chosen` and the window opens it like any other source (drop, Ctrl+O, CLI
all end up in `MainWindow.open_file`).
"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QIcon, QKeyEvent, QMouseEvent, QPainter, QPaintEvent, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .style import tokens
from .widgets.iconbutton import IconButton
from .widgets.icons import paint_icon

MEDIA_EXTS = frozenset(
    {
        ".mkv", ".mp4", ".webm", ".avi", ".mov", ".m4v", ".ts", ".m2ts", ".mpg",
        ".mpeg", ".wmv", ".flv", ".ogv", ".3gp",
        ".mp3", ".flac", ".aac", ".m4a", ".opus", ".ogg", ".wav", ".wma",
    }
)

_ICON_PX = 18
_SLIDE_MS = 170
_SLIDE_PX = 10  # drawer settle-in / slide-out
_FADE_MS = 90  # the fade carries the exit; the slide alone is only one inset
_ITEM_ROLE = int(Qt.ItemDataRole.UserRole)
_KIND_ROLE = _ITEM_ROLE + 1
_icons: dict[str, QIcon] = {}


def _icon(name: str, color: str) -> QIcon:
    """Cached QIcon built from the painted glyphs (no image files anywhere)."""
    key = f"{name}:{color}"
    cached = _icons.get(key)
    if cached is not None:
        return cached
    pixmap = QPixmap(_ICON_PX, _ICON_PX)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    paint_icon(painter, name, QRect(0, 0, _ICON_PX, _ICON_PX), color)
    painter.end()
    icon = QIcon(pixmap)
    _icons[key] = icon
    return icon


def start_directory(hint: str | None = None) -> Path:
    """Where the browser opens: the current file's folder, else last used, else home."""
    if hint:
        path = Path(hint)
        if path.is_dir():
            return path
        if path.parent.is_dir():
            return path.parent
    videos = Path.home() / "Videos"
    return videos if videos.is_dir() else Path.home()


class _Card(QWidget):
    """The drawer's card. Its own widget so the whole thing can be faded as one:
    a window-level paint cannot fade child widgets, and hiding a half-slid card is
    what made the old close look like it "moved a bit then vanished".

    It also paints the surface itself. The panel is a translucent top-level
    window, so without this the drawer is perfectly invisible over the (flat)
    home screen: same colour behind, no edges, nothing to see.
    """

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        radius = tokens.PANEL_RADIUS
        # Round only the inner (right) corners: the left side is the window edge,
        # so a rounded corner there would read as a floating pill again. Drawing the
        # rounded rect offset left of the widget clips the left corners flat.
        body = QRectF(-radius, 0.0, self.width() + radius, self.height())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(tokens.PANEL_BG))
        painter.drawRoundedRect(body, radius, radius)
        painter.setPen(QPen(QColor(tokens.PANEL_BORDER), 1.0))
        painter.drawLine(self.width() - 1, 0, self.width() - 1, self.height())
        painter.end()




class FilePanel(QWidget):
    file_chosen = Signal(str)
    visibility_changed = Signal(bool)
    menu_requested = Signal(QPoint)  # right click bubbles up to the player menu

    def __init__(self, host: QWidget) -> None:
        super().__init__(host, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self._host = host
        self.setObjectName("FilePanel")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # Show without activation (so the player keeps focus when the drawer opens
        # itself at startup) but still focusable by a click, because the list needs
        # the keyboard for Enter / Backspace / Esc.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowTitle("Browse")
        self.setMinimumSize(240, 200)
        self.setMouseTracking(True)

        self._root = start_directory()
        self._rest = QRect()
        self._slide = QPropertyAnimation(self, b"pos", self)
        self._slide.setDuration(_SLIDE_MS)
        self._slide.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._card = _Card(self)
        self._effect = QGraphicsOpacityEffect(self._card)
        self._effect.setOpacity(1.0)
        self._card.setGraphicsEffect(self._effect)
        self._fade = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade.setDuration(_FADE_MS)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.finished.connect(self._on_fade_finished)
        self._closing_slide = False

        self._up = IconButton("up", self, size=tokens.BTN_SM, icon_size=20, tip="Up one folder")
        self._path = QLabel(self)
        self._path.setObjectName("PanelPath")
        self._path.setMinimumWidth(80)
        self._close = IconButton("close", self, size=tokens.BTN_SM, icon_size=20, tip="Close (Esc)")

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        header.addWidget(self._up)
        header.addWidget(self._path, 1)
        header.addWidget(self._close)

        self._list = QListWidget(self)
        self._list.setObjectName("FileList")
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self._list.setUniformItemSizes(True)
        self._list.setMouseTracking(True)

        self._hint = QLabel("Double-click a folder · double-click a file to play", self)
        self._hint.setObjectName("PanelHint")

        layout = QVBoxLayout(self._card)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addLayout(header)
        layout.addWidget(self._list, 1)
        layout.addWidget(self._hint)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._card)

        self._up.clicked.connect(self.go_up)
        self._close.clicked.connect(self.close_panel)
        self._list.itemActivated.connect(self._activate)

        self._host.installEventFilter(self)
        self.sync_geometry()

    # ------------------------------------------------------------------- api
    def is_open(self) -> bool:
        return self.isVisible()

    def open_at(self, hint: str | None = None) -> None:
        target = start_directory(hint)
        if target != self._root or self._list.count() == 0:
            self.set_root(target)
        self.sync_geometry()
        # Also "opening" when the drawer is still sliding out: otherwise a reopen
        # during the out animation gets hidden when that animation finishes.
        opening = not self.isVisible() or self._closing_slide
        self._closing_slide = False
        self.show()
        self.raise_()
        if opening:
            self._slide_in()
        self.visibility_changed.emit(True)

    def close_panel(self) -> None:
        if self.isVisible() and not self._closing_slide:
            self._slide_out()

    def toggle(self, hint: str | None = None) -> None:
        if self.isVisible():
            self.close_panel()
        else:
            self.open_at(hint)

    def set_root(self, directory: Path | str) -> None:
        root = Path(directory)
        if not root.is_dir():
            root = root.parent
        self._root = root
        self._path.setText(self._elided(root.name or str(root)))
        self._path.setToolTip(str(root))
        self._up.setEnabled(root.parent != root)
        self._fill()

    def current_directory(self) -> str:
        return str(self._root)

    # -------------------------------------------------------------- geometry
    def sync_geometry(self) -> None:
        """A sidebar, not a pill: flush to the window edge and full height."""
        client = self._host.rect()
        width = min(tokens.PANEL_W, max(220, client.width() // 2))
        origin = self._host.mapToGlobal(client.topLeft())
        self._rest = QRect(origin.x(), origin.y(), width, max(200, client.height()))
        if self._slide.state() != QPropertyAnimation.State.Running:
            self.setGeometry(self._rest)

    def _flush_left(self) -> QPoint:
        """Start position: mostly off the window's left edge.

        The slide is short on purpose - a top-level window cannot be clipped by its
        parent, so a long slide would hang over the desktop beside the player.
        """
        return QPoint(self._rest.x() - _SLIDE_PX, self._rest.y())

    def _slide_in(self) -> None:
        # Starts partly opaque so the rows are readable immediately; a fade from
        # zero made the drawer look empty for the first frames.
        self._closing_slide = False
        self.move(self._flush_left())
        self._run(self._flush_left(), self._rest.topLeft(), 0.55, 1.0)

    def _slide_out(self) -> None:
        self._closing_slide = True
        self._run(self.pos(), self._flush_left(), self._effect.opacity(), 0.0)

    def _run(self, start: QPoint, end: QPoint, alpha_from: float, alpha_to: float) -> None:
        self._slide.stop()
        self._slide.setStartValue(start)
        self._slide.setEndValue(end)
        self._slide.start()
        self._fade.stop()
        self._fade.setStartValue(float(alpha_from))
        self._fade.setEndValue(float(alpha_to))
        self._fade.start()

    def _on_fade_finished(self) -> None:
        if self._closing_slide:
            self._closing_slide = False
            self.hide()

    def sizeHint(self) -> QSize:
        return QSize(tokens.PANEL_W, 480)

    def _elided(self, text: str) -> str:
        metrics = self._path.fontMetrics()
        return metrics.elidedText(text, Qt.TextElideMode.ElideMiddle, tokens.PANEL_W - 130)

    # ---------------------------------------------------------------- listing
    def _fill(self) -> None:
        self._list.clear()
        try:
            entries = list(os.scandir(self._root))
        except OSError:
            entries = []
        folders: list[os.DirEntry] = []
        files: list[os.DirEntry] = []
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir():
                    folders.append(entry)
                elif os.path.splitext(entry.name)[1].lower() in MEDIA_EXTS:
                    files.append(entry)
            except OSError:
                continue
        folders.sort(key=lambda item: item.name.casefold())
        files.sort(key=lambda item: item.name.casefold())

        for entry in folders:
            self._append(entry.name, "folder", entry.path)
        for entry in files:
            self._append(entry.name, "file", entry.path)

        if not folders and not files:
            item = QListWidgetItem("Nothing playable in here")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._list.addItem(item)

    def _append(self, name: str, kind: str, path: str) -> None:
        item = QListWidgetItem(_icon(kind, tokens.TEXT_DIM), name)
        item.setData(_ITEM_ROLE, path)
        item.setData(_KIND_ROLE, kind)
        if kind == "file":
            item.setToolTip(name)
        self._list.addItem(item)

    # ----------------------------------------------------------------- events
    def _activate(self, item: QListWidgetItem) -> None:
        path = item.data(_ITEM_ROLE)
        if not path:
            return
        if item.data(_KIND_ROLE) == "folder":
            self.set_root(path)
            return
        self.file_chosen.emit(str(path))

    def go_up(self) -> None:
        parent = self._root.parent
        if parent != self._root and parent.is_dir():
            # Keep the folder we came from selected: walking up and down again is
            # the most common thing to do in a browser.
            previous = self._root.name
            self.set_root(parent)
            for row in range(self._list.count()):
                item = self._list.item(row)
                if item.data(_KIND_ROLE) == "folder" and item.text() == previous:
                    self._list.setCurrentItem(item)
                    break

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close_panel()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Backspace:
            self.go_up()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            item = self._list.currentItem()
            if item is not None:
                self._activate(item)
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        # Same gesture as the strip: clicking empty panel space does nothing, but a
        # right click anywhere should still reach the player menu.
        if event.button() == Qt.MouseButton.RightButton:
            self.menu_requested.emit(event.globalPosition().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def showEvent(self, event) -> None:  # noqa: ANN001 - QShowEvent
        super().showEvent(event)
        self.visibility_changed.emit(True)

    def hideEvent(self, event) -> None:  # noqa: ANN001 - QHideEvent
        super().hideEvent(event)
        self.visibility_changed.emit(False)

    def eventFilter(self, watched, event) -> bool:  # noqa: ANN001 - Qt signature
        if watched is self._host:
            kind = event.type()
            if kind in (QEvent.Type.Move, QEvent.Type.Resize) and self.isVisible():
                self.sync_geometry()
            elif kind in (QEvent.Type.Hide, QEvent.Type.Close):
                self.hide()
        return super().eventFilter(watched, event)




__all__ = ["FilePanel", "MEDIA_EXTS", "start_directory"]
