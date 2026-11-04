"""Search and download subtitles from OpenSubtitles, from inside the player.

Threading: `subtitle_hash()` reads 128 KiB and every API call is a network round
trip, so all of it runs on a plain Python thread and reports back through the
dialog's own signals. Qt queues those for the GUI thread, which is why the window
never blocks and never touches libVLC or widgets off-thread. (A QRunnable was the
obvious choice and it bit us: the pool deletes the runnable, and the signals
object went with it - "Signal source has been deleted" mid-download.)

Saving: subtitles land next to the video as `<name>.<lang>.srt` (that is also
the name VLC and our own auto-load look for), falling back to
`%LOCALAPPDATA%/Reel/subtitles` when the folder is read-only - a USB stick or a
network share people cannot write to.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .settings import Settings
from .style import tokens
from .subtitles_api import (
    API_KEY_SETTING,
    OpenSubtitles,
    SubtitleError,
    SubtitleResult,
    clean_query,
    subtitle_hash,
)
from .win_dwm import apply_dark_titlebar

LANGUAGES_HINT = "en, es, fr"


def _fallback_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "Reel" / "subtitles"


class SubtitleSearch(QDialog):
    visibility_changed = Signal(bool)
    subtitle_ready = Signal(str)  # local path of a downloaded subtitle
    _job_done = Signal(object)
    _job_failed = Signal(str)

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._video: Path | None = None
        self._results: list[SubtitleResult] = []
        self._year: int | None = None
        self._thread: threading.Thread | None = None
        # Callbacks for the one job that may be running. Named with a "job_"
        # prefix on purpose: `_on_failed` is also a method, and an instance
        # attribute of the same name silently shadows it.
        self._job_on_done = None
        self._job_on_fail = None
        self._themed = False
        self._job_done.connect(self._deliver)
        self._job_failed.connect(self._deliver_failure)

        self.setObjectName("SubtitleSearch")
        self.setWindowTitle("Find subtitles")
        self.setModal(False)
        self.resize(560, 460)
        self.setMinimumSize(460, 340)

        self._query = QLineEdit(self)
        self._query.setObjectName("SearchField")
        self._query.setPlaceholderText("Film or series name")
        self._query.returnPressed.connect(self._search)

        self._languages = QLineEdit(self)
        self._languages.setObjectName("SearchField")
        self._languages.setFixedWidth(120)
        self._languages.setPlaceholderText(LANGUAGES_HINT)
        self._languages.returnPressed.connect(self._search)

        self._go = QPushButton("Search", self)
        self._go.setObjectName("PromptPrimary")
        self._go.setCursor(Qt.CursorShape.PointingHandCursor)
        self._go.clicked.connect(self._search)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self._query, 1)
        row.addWidget(self._languages)
        row.addWidget(self._go)

        self._list = QListWidget(self)
        self._list.setObjectName("ResultList")
        self._list.setUniformItemSizes(False)
        self._list.itemDoubleClicked.connect(lambda _item: self._download())

        self._status = QLabel("", self)
        self._status.setObjectName("StatusNote")
        self._status.setWordWrap(True)

        self._download_btn = QPushButton("Download", self)
        self._download_btn.setObjectName("PromptPrimary")
        self._download_btn.setEnabled(False)
        self._download_btn.clicked.connect(self._download)

        close_btn = QPushButton("Close", self)
        close_btn.setObjectName("PromptSecondary")
        close_btn.clicked.connect(self.close)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        footer.addWidget(self._status, 1)
        footer.addWidget(self._download_btn)
        footer.addWidget(close_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 14)
        layout.setSpacing(10)
        layout.addLayout(row)
        layout.addWidget(self._list, 1)
        layout.addLayout(footer)

        self._list.currentRowChanged.connect(
            lambda row_index: self._download_btn.setEnabled(
                row_index >= 0 and bool(self._results) and not self._busy()
            )
        )
        self._settings.changed.connect(self._on_setting_changed)

    # ------------------------------------------------------------------ api
    def open_for(self, video: str | Path) -> None:
        """Point the dialog at a video and search for it straight away."""
        self._video = Path(video)
        title, year = clean_query(self._video.name)
        self._year = year
        self._query.setText(title)
        languages = str(self._settings.get("sub.languages") or "en")
        if not self._languages.text().strip():
            self._languages.setText(languages)
        self._results = []
        self._list.clear()
        self._status.setText("")
        self._search()

    def _client(self) -> OpenSubtitles:
        return OpenSubtitles(str(self._settings.get(API_KEY_SETTING) or ""))

    def _busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _on_setting_changed(self, key: str) -> None:
        if key == "sub.languages" and not self._languages.text().strip():
            self._languages.setText(str(self._settings.get("sub.languages")))

    # -------------------------------------------------------------- searching
    def _search(self) -> None:
        query = self._query.text().strip()
        if not query:
            self._status.setText("Type a film or series name first.")
            return
        if self._busy():
            return
        languages = self._languages.text().strip() or "en"
        self._settings.set("sub.languages", languages)  # remembered for next time
        self._status.setText("Searching\u2026")
        self._download_btn.setEnabled(False)
        self._list.clear()
        self._results = []
        client = self._client()
        video = self._video
        year = self._year

        def work() -> list[SubtitleResult]:
            # Hash first: a hash hit is timed for this exact release, which beats
            # any name-based match. A miss (UnknownError/406) is not a failure -
            # fall through to the plain title search.
            if video is not None and video.exists():
                try:
                    digest, size = subtitle_hash(video)
                except (SubtitleError, OSError):
                    digest = ""
                else:
                    try:
                        hits = client.search(query, languages, movie_hash=digest, movie_size=size)
                    except SubtitleError as error:
                        if error.status in (406, 404, 429):
                            raise
                        hits = []
                    if hits:
                        return hits
            return client.search(query, languages, year=year)

        self._run(work, self._on_results, self._on_failed)

    def _on_results(self, results: list[SubtitleResult]) -> None:
        if not results:
            self._results = []
            self._status.setText("No subtitles found. Try a shorter title or more languages.")
            return
        # Best first: non-HI before HI, then the most downloaded, so row 1 is the
        # one that usually works without thinking.
        self._results = sorted(results, key=lambda item: (item.hearing_impaired, -item.downloads))
        results = self._results
        for index, result in enumerate(results, start=1):
            item = QListWidgetItem(f"{index}.  {result.label()}\n     {result.release or result.filename}")
            item.setToolTip(result.release or result.filename)
            self._list.addItem(item)
        self._status.setText(f"{len(results)} result{'s' if len(results) != 1 else ''}.")
        self._list.setCurrentRow(0)

    def _on_failed(self, message: str) -> None:
        self._status.setText(message)
        self._download_btn.setEnabled(False)

    # ------------------------------------------------------------- downloading
    def _selected(self) -> SubtitleResult | None:
        row = self._list.currentRow()
        if 0 <= row < len(self._results):
            return self._results[row]
        return None

    def _download(self) -> None:
        result = self._selected()
        if result is None or self._busy():
            return
        video = self._video
        if video is None:
            self._status.setText("Open a video first.")
            return
        client = self._client()
        folder = video.parent
        stem = video.stem
        self._status.setText("Downloading\u2026")
        self._download_btn.setEnabled(False)

        def work() -> Path:
            try:
                return client.save(result.file_id, folder, stem, result.language)
            except SubtitleError as error:
                if error.status in (401, 403, 429):
                    raise  # key and quota problems are not about the folder
            except OSError:
                pass
            return client.save(result.file_id, _fallback_dir(), stem, result.language)

        self._run(work, self._on_downloaded, self._on_failed)

    def _on_downloaded(self, path: Path) -> None:
        self._status.setText(f"Saved {path.name}")
        self.subtitle_ready.emit(str(path))
        self.close()

    # ------------------------------------------------------------------ jobs
    def _run(self, work, on_done, on_failed) -> None:  # noqa: ANN001
        self._job_on_done, self._job_on_fail = on_done, on_failed
        self._thread = threading.Thread(target=self._work, args=(work,), daemon=True)
        self._thread.start()

    def _work(self, work) -> None:  # noqa: ANN001 - runs off the GUI thread
        try:
            value = work()
        except SubtitleError as error:
            self._job_failed.emit(str(error))
        except Exception as error:  # noqa: BLE001 - a thread must never die silently
            self._job_failed.emit(f"{type(error).__name__}: {error}")
        else:
            self._job_done.emit(value)

    def _deliver(self, value) -> None:  # noqa: ANN001 - GUI thread
        done, self._job_on_done = self._job_on_done, None
        if done is not None:
            done(value)
        self._refresh_buttons()

    def _deliver_failure(self, message: str) -> None:
        failed, self._job_on_fail = self._job_on_fail, None
        self._job_on_done = None
        if failed is not None:
            failed(message)
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        has_row = self._list.currentRow() >= 0 and bool(self._results)
        self._download_btn.setEnabled(has_row and not self._busy())

    # ---------------------------------------------------------------- events
    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not self._themed:
            self._themed = True
            apply_dark_titlebar(self, tokens.TITLEBAR, tokens.TEXT, tokens.TITLEBAR)
        self.visibility_changed.emit(True)
        self._query.setFocus()

    def hideEvent(self, event) -> None:  # noqa: ANN001 - Qt signature
        self.visibility_changed.emit(False)
        super().hideEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: ANN001 - Qt signature
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)


__all__ = ["SubtitleSearch"]
