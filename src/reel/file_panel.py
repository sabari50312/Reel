"""Side file browser - the drawer that replaces the "open a file" dialog.

Why it is a separate top-level window: the picture is a native child HWND that
Windows composites above Qt's own painting, so a panel inside the main window
would be covered by the video. Same trick as the control strip, positioned over
the right edge of the client area instead of the bottom.

Navigation is a real browser's, not a ladder: a *Home* button, a *quick places*
menu (Home/Videos/Desktop/Downloads + drives + recently visited folders), a
back/forward history, and an editable address bar that takes a typed or pasted
path (quotes, `~`, `%VARS%`, forward slashes and a bare `D:` are all understood).
Folders open on a single click; files need Enter or a double click.

It only browses: `os.scandir` of one folder at a time, folders first, media files
filtered by extension. Activating a file emits `file_chosen` and the window opens
it like any other source (drop, Ctrl+O, CLI all end up in `MainWindow.open_file`).
`directory_changed` lets the window remember where browsing stopped.
"""

from __future__ import annotations

import os
import time
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
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QIcon, QKeyEvent, QMouseEvent, QPainter, QPaintEvent, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
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

_NAV_BTN = 28  # header: five of these plus the close button need the room
_NAV_ICON = 16
_STATUS_MS = 2600  # how long a rejected path stays in the hint line
_NAV_GUARD_S = 0.35  # ignore the tail of a folder double-click
_HISTORY_LIMIT = 64
_HINT = "Click a folder · Enter or double-click a file"


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


def normalise_path(value: str | Path) -> Path | None:
    """Address-bar text -> Path, or None when nothing usable is in there.

    People paste paths with surrounding quotes, `%USERPROFILE%`, forward slashes
    (from WSL or a browser), or just a drive letter - all of those are accepted,
    because a path field that rejects what Windows itself accepts is worse than
    no path field at all.
    """
    text = str(value).strip().strip("\"'").strip()
    if not text:
        return None
    text = os.path.expandvars(os.path.expanduser(text))
    text = text.replace("/", os.sep)
    if len(text) == 2 and text[1] == ":":
        text += os.sep  # "D:" is the drive's *current* folder; "D:\" is the root
    try:
        return Path(text)
    except (OSError, ValueError):
        return None


def quick_places() -> list[tuple[str, Path]]:
    """The folders a file browser is actually asked for, if they exist."""
    home = Path.home()
    wanted = (
        ("Home", home),
        ("Videos", home / "Videos"),
        ("Desktop", home / "Desktop"),
        ("Downloads", home / "Downloads"),
        ("Music", home / "Music"),
        ("Pictures", home / "Pictures"),
    )
    return [(label, path) for label, path in wanted if path.is_dir()]


def available_drives() -> list[str]:
    """Root folder of every mounted drive (`os.listdrives` is 3.12+)."""
    lister = getattr(os, "listdrives", None)
    if lister is None:
        return []
    try:
        return list(lister())
    except OSError:
        return []


def _shorten(path: Path, limit: int = 46) -> str:
    text = str(path)
    return text if len(text) <= limit else "\u2026" + text[-(limit - 1):]


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
    directory_changed = Signal(str)  # browsing moved: remember it for next time

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
        # Browsing history: the whole reason back/forward exists is that "one
        # folder up" is rarely the folder you want.
        self._history: list[Path] = []
        self._hist_index = -1
        self._nav_time = 0.0
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

        # Header: places / home / up / back / forward, then the editable address
        # bar on its own line so a deep path is readable instead of a sliver.
        self._places = IconButton(
            "chevron_down", self, size=24, icon_size=14, tip="Quick places"
        )
        self._home = IconButton(
            "home", self, size=_NAV_BTN, icon_size=_NAV_ICON, tip="Home folder"
        )
        self._up = IconButton(
            "up", self, size=_NAV_BTN, icon_size=_NAV_ICON, tip="Up one folder (Backspace)"
        )
        self._back = IconButton(
            "back", self, size=_NAV_BTN, icon_size=_NAV_ICON, tip="Back (Alt+Left)"
        )
        self._forward = IconButton(
            "forward", self, size=_NAV_BTN, icon_size=_NAV_ICON, tip="Forward (Alt+Right)"
        )
        self._close = IconButton(
            "close", self, size=_NAV_BTN, icon_size=_NAV_ICON, tip="Close (Esc)"
        )

        self._address = QLineEdit(self)
        self._address.setObjectName("PanelAddress")
        self._address.setPlaceholderText("Path — Enter to go")
        self._address.setToolTip("Click to type a path, or press Ctrl+L")
        self._address.setClearButtonEnabled(False)
        # Reads as the current path until it is clicked. A field that is always
        # ready to type in looks like a search box and steals focus from the list.
        self._address.setReadOnly(True)
        self._address.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self._address.installEventFilter(self)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(2)
        for button in (self._places, self._home, self._up, self._back, self._forward):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self._close)

        header = QVBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        header.addLayout(buttons)
        header.addWidget(self._address)

        self._list = QListWidget(self)
        self._list.setObjectName("FileList")
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self._list.setUniformItemSizes(True)
        self._list.setMouseTracking(True)
        self._list.installEventFilter(self)

        self._hint = QLabel(_HINT, self)
        self._hint.setObjectName("PanelHint")

        # A rejected path reports itself here for a couple of seconds, then the
        # hint comes back: a dialog for a typo would be far worse than a red line.
        self._status = QTimer(self)
        self._status.setSingleShot(True)
        self._status.setInterval(_STATUS_MS)
        self._status.timeout.connect(self._restore_hint)

        layout = QVBoxLayout(self._card)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addLayout(header)
        layout.addWidget(self._list, 1)
        layout.addWidget(self._hint)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._card)

        self._places.clicked.connect(self._show_places)
        self._home.clicked.connect(self.go_home)
        self._up.clicked.connect(self.go_up)
        self._back.clicked.connect(self.go_back)
        self._forward.clicked.connect(self.go_forward)
        self._close.clicked.connect(self.close_panel)
        self._address.returnPressed.connect(self._on_address_entered)
        self._address.textEdited.connect(lambda _text: self._clear_status())
        self._list.itemActivated.connect(self._activate)
        self._list.itemClicked.connect(self._on_item_clicked)
        self._sync_history()

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

    def set_root(
        self,
        directory: Path | str,
        *,
        record: bool = True,
        select: str | None = None,
    ) -> bool:
        """Show `directory`. False (and the current folder stays) when it is gone."""
        root = normalise_path(directory)
        if root is None or not root.is_dir():
            return False
        self._root = root
        self._address.setText(str(root))
        # Caret at the end: a long path is identified by its tail, and an
        # unfocused QLineEdit shows the text around the caret.
        self._address.setCursorPosition(len(self._address.text()))
        self._address.setToolTip(str(root))
        self._up.setEnabled(root.parent != root)
        self._nav_time = time.monotonic()
        self._fill()
        if record:
            self._push_history(root)
        if select:
            self._select_entry(select)
        self.directory_changed.emit(str(root))
        return True

    def current_directory(self) -> str:
        return str(self._root)

    def refresh(self) -> None:
        self._fill()

    # --------------------------------------------------------------- history
    def _push_history(self, root: Path) -> None:
        if 0 <= self._hist_index < len(self._history) and self._history[self._hist_index] == root:
            self._sync_history()
            return
        del self._history[self._hist_index + 1:]  # going somewhere new drops the branch
        self._history.append(root)
        if len(self._history) > _HISTORY_LIMIT:
            del self._history[0]
        self._hist_index = len(self._history) - 1
        self._sync_history()

    def _sync_history(self) -> None:
        self._back.setEnabled(self._hist_index > 0)
        self._forward.setEnabled(0 <= self._hist_index < len(self._history) - 1)

    def go_back(self) -> None:
        if self._hist_index <= 0:
            return
        came_from = self._root.name
        self._hist_index -= 1
        self.set_root(self._history[self._hist_index], record=False, select=came_from)
        self._sync_history()

    def go_forward(self) -> None:
        if not 0 <= self._hist_index < len(self._history) - 1:
            return
        came_from = self._root.name
        self._hist_index += 1
        self.set_root(self._history[self._hist_index], record=False, select=came_from)
        self._sync_history()

    def go_home(self) -> None:
        if not self.set_root(Path.home()):
            self._flash("The home folder is not reachable")

    def go_up(self) -> None:
        parent = self._root.parent
        if parent != self._root and parent.is_dir():
            # Keep the folder we came from selected: walking up and down again is
            # the most common thing to do in a browser.
            self.set_root(parent, select=self._root.name)

    # --------------------------------------------------------------- address
    def focus_address(self) -> None:
        self._begin_edit()
        self._address.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self._address.selectAll()

    def _begin_edit(self) -> None:
        """Unlock the path bar: it is the current path until the user wants it."""
        self._address.setReadOnly(False)
        self._address.setToolTip("Enter to go, Esc to cancel")

    def _end_edit(self) -> None:
        self._address.setReadOnly(True)
        self._address.setToolTip("Click to type a path, or press Ctrl+L")
        self._address.setText(str(self._root))
        self._address.setCursorPosition(len(self._address.text()))
        self._clear_status()

    def _on_address_entered(self) -> None:
        target = normalise_path(self._address.text())
        if target is None:
            self._reject_address("Type a folder or file path")
            return
        if target.is_file():
            if target.suffix.lower() in MEDIA_EXTS:
                self.file_chosen.emit(str(target))
            else:
                self._reject_address("That is not a media file")
            return
        if not self.set_root(target):
            self._reject_address("No such folder")
            return
        # Hand the keyboard back to the listing: after jumping somewhere the next
        # useful keys are arrows / Enter / Backspace, not more typing.
        self._list.setFocus()

    def _reject_address(self, message: str) -> None:
        self._flash(message)
        self._address.setProperty("error", True)
        self._repolish(self._address)
        self.focus_address()

    def _revert_address(self) -> None:
        self._clear_status()
        self._address.setText(str(self._root))
        self._address.setCursorPosition(len(self._address.text()))
        self._list.setFocus()

    def _flash(self, message: str) -> None:
        self._hint.setText(message)
        self._hint.setProperty("error", True)
        self._repolish(self._hint)
        self._status.start()

    def _clear_status(self) -> None:
        if self._status.isActive():
            self._status.stop()
            self._restore_hint()

    def _restore_hint(self) -> None:
        self._hint.setText(_HINT)
        self._hint.setProperty("error", False)
        self._repolish(self._hint)
        self._address.setProperty("error", False)
        self._repolish(self._address)

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        """Re-run the QSS property selectors after a dynamic property changed."""
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)
        widget.update()

    # ---------------------------------------------------------------- places
    def _show_places(self) -> None:
        menu = QMenu(self)
        folder_icon = _icon("folder", tokens.TEXT_DIM)
        for label, folder in quick_places():
            action = menu.addAction(folder_icon, label)
            action.triggered.connect(lambda _checked=False, path=folder: self.set_root(path))
        drives = available_drives()
        if drives:
            menu.addSeparator()
            for drive in drives:
                action = menu.addAction(folder_icon, drive)
                action.triggered.connect(lambda _checked=False, path=drive: self.set_root(path))
        recent = [path for path in reversed(self._history) if path != self._root][:8]
        if recent:
            menu.addSeparator()
            heading = menu.addAction("Recent")
            heading.setEnabled(False)
            for path in recent:
                action = menu.addAction(folder_icon, _shorten(path))
                action.setToolTip(str(path))
                action.triggered.connect(lambda _checked=False, p=path: self.set_root(p))
        menu.exec(self._places.mapToGlobal(self._places.rect().bottomLeft()))

    def _select_entry(self, name: str) -> None:
        """Pre-select the folder we arrived from, so the trip is reversible."""
        for row in range(self._list.count()):
            item = self._list.item(row)
            if item.data(_KIND_ROLE) == "folder" and item.text() == name:
                self._list.setCurrentItem(item)
                self._list.scrollToItem(item)
                return

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

    # ---------------------------------------------------------------- listing
    def _fill(self) -> None:
        self._list.clear()
        try:
            entries = list(os.scandir(self._root))
        except OSError as error:
            reason = error.strerror or "access denied"
            self._empty(f"Can't open this folder ({reason})")
            return
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
            self._empty("Nothing playable in here")

    def _empty(self, text: str) -> None:
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        self._list.addItem(item)

    def _append(self, name: str, kind: str, path: str) -> None:
        item = QListWidgetItem(_icon(kind, tokens.TEXT_DIM), name)
        item.setData(_ITEM_ROLE, path)
        item.setData(_KIND_ROLE, kind)
        item.setToolTip(path)
        self._list.addItem(item)

    # ----------------------------------------------------------------- events
    def _navigable(self) -> bool:
        """False for the instant right after a folder opened.

        Only `itemActivated` is guarded. A double click on a folder would open it
        and then activate whatever row landed under the pointer in the new listing
        (entering another folder, or starting a film) - that is the bug that makes
        clicking a folder feel finicky. Single clicks stay unguarded: clicking
        through three folders quickly is a legit way to browse, and the second
        press of a double click never arrives as a click, only as an activation.
        """
        return (time.monotonic() - self._nav_time) > _NAV_GUARD_S

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        # Folders open on one click; files need Enter or a double click, so a stray
        # click in a full folder never starts a film by accident.
        if item.data(_KIND_ROLE) == "folder":
            path = item.data(_ITEM_ROLE)
            if path:
                self.set_root(path)

    def _activate(self, item: QListWidgetItem) -> None:
        path = item.data(_ITEM_ROLE)
        if not path or not self._navigable():
            return
        if item.data(_KIND_ROLE) == "folder":
            self.set_root(path)
            return
        self.file_chosen.emit(str(path))

    def keyPressEvent(self, event: QKeyEvent) -> None:
        # Anything pressed while the panel itself (not the list) holds the focus
        # takes the same shortcuts as the list.
        if self._handle_nav_key(event):
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            item = self._list.currentItem()
            if item is not None:
                self._activate(item)
            event.accept()
            return
        super().keyPressEvent(event)

    def _handle_nav_key(self, event: QKeyEvent) -> bool:
        """Shortcuts that must work while the folder list holds the focus."""
        key = event.key()
        modifiers = event.modifiers()
        if key == Qt.Key.Key_Backspace:
            self.go_up()
            return True
        if key == Qt.Key.Key_F5:
            self.refresh()
            return True
        if key == Qt.Key.Key_L and modifiers & Qt.KeyboardModifier.ControlModifier:
            self.focus_address()
            return True
        if key == Qt.Key.Key_Escape:
            self.close_panel()
            return True
        if modifiers & Qt.KeyboardModifier.AltModifier:
            if key == Qt.Key.Key_Left:
                self.go_back()
                return True
            if key == Qt.Key.Key_Right:
                self.go_forward()
                return True
            if key == Qt.Key.Key_Up:
                self.go_up()
                return True
        return False

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
        elif watched is self._list and event.type() == QEvent.Type.KeyPress:
            if self._handle_nav_key(event):
                return True
        elif watched is self._address and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            # Esc in the address bar means "forget my typing", not "close the
            # browser": it is a field first and a navigation tool second.
            if key == Qt.Key.Key_Escape:
                self._revert_address()
                return True
            if key == Qt.Key.Key_Down:
                self._list.setFocus()
                return True
        elif watched is self._address and event.type() == QEvent.Type.FocusIn:
            self._begin_edit()
            reason = getattr(event, "reason", lambda: None)()
            if reason in (
                Qt.FocusReason.ShortcutFocusReason,
                Qt.FocusReason.TabFocusReason,
            ):
                # Arrived here by Ctrl+L: the whole path is what usually gets
                # replaced. A plain click leaves the caret where it landed.
                self._address.selectAll()
        elif watched is self._address and event.type() == QEvent.Type.FocusOut:
            self._end_edit()
        return super().eventFilter(watched, event)


__all__ = [
    "FilePanel",
    "MEDIA_EXTS",
    "available_drives",
    "normalise_path",
    "quick_places",
    "start_directory",
]
