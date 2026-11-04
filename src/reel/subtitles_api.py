"""OpenSubtitles.com REST client (API v1).

Why a hand-written client instead of a dependency: the whole surface we need is
two endpoints and a GET, it is stdlib-only (`urllib`), and it keeps the exe free
of `requests`/`beautifulsoup`/`guessit` for one feature.

Contract (api.opensubtitles.com/api/v1):
  GET  /subtitles?query=&languages=&type=movie|tvshow&moviehash=&moviebytesize=
       headers: Api-Key, User-Agent
       -> {"data": [{"attributes": {language, release, download_count, ratings,
                                    feature_details: {title, year},
                                    files: [{file_id, file_name}]}}]}
  POST /download  {"file_id": 123, "sub_format": "srt"}
       -> {"link": "...", "file_name": "...", "remaining": 19}
  GET  <link>   -> the subtitle file itself (always UTF-8)

Two things that are easy to get wrong and are handled here: the API key travels
in a *header* (not a query parameter), and the download is two requests - the
POST only returns a temporary link. Failures raise `SubtitleError` with a message
meant for the status label, never a raw traceback.

The transport is injectable so the parsing, hashing and error mapping can be
tested without a network or an API key.
"""

from __future__ import annotations

import gzip
import json
import re
import struct
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

API_ROOT = "https://api.opensubtitles.com/api/v1"
API_KEY_SETTING = "sub.api_key"
USER_AGENT = "Reel v0.1"
TIMEOUT_S = 15.0
_HASH_CHUNK = 65536  # the OpenSubtitles hash reads the first and last 64 KiB

Transport = Callable[[str, str, dict[str, str], "bytes | None", float], tuple[int, bytes]]

# Noise that survives in release names but says nothing about *which* film it is.
_NOISE = re.compile(
    r"\b(2160p|1080p|720p|480p|4k|uhd|hdr|hdr10|dv|dolby\.?vision|bluray|blu-ray|brrip|"
    r"bdrip|webrip|web-dl|webdl|web|hdtv|dvdrip|dvd|xvid|divx|hevc|h\.?26[45]|x26[45]|"
    r"av1|10bit|8bit|hq|proper|repack|extended|remastered|uncut|imax|aac\d?|ac3|eac3|"
    r"dts(-hd)?|truehd|atmos|5\.1|7\.1|2\.0|multi|dual|subbed|dubbed|amzn|nf|dsnp|hmax)\b",
    re.IGNORECASE,
)
_TAG = re.compile(r"[\[\(\{][^\]\)\}]*[\]\)\}]")
_YEAR = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")


class SubtitleError(Exception):
    """A failure with a message that can go straight into the UI."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class SubtitleResult:
    file_id: int
    language: str
    title: str
    year: int | None
    release: str
    downloads: int
    rating: float
    hearing_impaired: bool
    filename: str

    def label(self) -> str:
        """One line for the results list: what it is, then how good it is."""
        bits = [self.title or self.filename or "Untitled"]
        if self.year:
            bits[0] = f"{bits[0]} ({self.year})"
        bits.append(self.language.upper())
        if self.hearing_impaired:
            bits.append("HI")
        if self.rating:
            bits.append(f"{self.rating:.1f}\u2605")
        bits.append(f"{self.downloads:,} downloads")
        return "  \u00b7  ".join(bits)


# --------------------------------------------------------------------- names
def clean_query(name: str) -> tuple[str, int | None]:
    """'Caminandes.Gran.Dillama.2013.1080p.WEB-DL.x264-GRP.mkv' -> ("Caminandes Gran Dillama", 2013)."""
    raw = Path(name).stem
    raw = re.sub(r"[._]+", " ", raw)
    # The year is read *before* bracketed tags are dropped, because it is usually
    # inside them: "Interstellar (2014) [2160p]".
    year_match = _YEAR.search(raw)
    year = int(year_match.group(1)) if year_match else None

    text = _TAG.sub(" ", raw)
    text = _NOISE.sub(" ", text)
    if year_match:
        text = text.replace(year_match.group(1), " ")
    # Trailing release group: "... WEB-DL x264 -GRP" / "...-YTS".
    text = re.sub(r"(?:\s*-\s*[A-Za-z0-9]{2,12})\s*$", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -_")
    return (text or Path(name).stem or name), year


def languages_param(value: str) -> str:
    """'en, ES fr' -> 'en,es,fr'; OpenSubtitles wants ISO 639-1 codes only."""
    codes = [part.strip().lower() for part in re.split(r"[,\s]+", value or "")]
    return ",".join(dict.fromkeys(code for code in codes if code))


# ---------------------------------------------------------------------- hash
def subtitle_hash(path: str | Path) -> tuple[str, int]:
    """OpenSubtitles' movie hash: first+last 64 KiB plus the file size.

    Exact enough that a hash match means the subtitle was timed for this very
    release, which is the difference between synced and unwatchable.
    """
    file_path = Path(path)
    size = file_path.stat().st_size
    if size < _HASH_CHUNK * 2:
        raise SubtitleError("File is too small for a subtitle hash.")
    value = size
    with file_path.open("rb") as handle:
        for position in (0, max(0, size - _HASH_CHUNK)):
            handle.seek(position)
            block = handle.read(_HASH_CHUNK)
            value += sum(struct.unpack("<%dQ" % (len(block) // 8), block))
    return f"{value & 0xFFFFFFFFFFFFFFFF:016x}", size


# -------------------------------------------------------------------- client
# -------------------------------------------------------------------- client
def _http(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as error:
        return int(error.code), error.read() or b""
    except urllib.error.URLError as error:
        raise SubtitleError(f"Could not reach OpenSubtitles ({error.reason}).") from error
    except TimeoutError as error:
        raise SubtitleError("OpenSubtitles did not answer in time.") from error


_STATUS_MESSAGES = {
    401: "OpenSubtitles rejected the API key.",
    403: "OpenSubtitles refused the request - check the API key.",
    406: "No subtitles found for that search.",
    429: "Daily download limit reached for this key.",
    503: "OpenSubtitles is temporarily unavailable.",
}


class OpenSubtitles:
    def __init__(self, api_key: str, transport=_http, timeout: float = TIMEOUT_S) -> None:
        self._api_key = (api_key or "").strip()
        self._transport = transport
        self._timeout = timeout

    # ---------------------------------------------------------------- plumbing
    def _headers(self, post: bool = False) -> dict[str, str]:
        headers = {"Api-Key": self._api_key, "User-Agent": USER_AGENT, "Accept": "application/json"}
        if post:
            headers["Content-Type"] = "application/json"
        return headers

    def _call(self, method: str, url: str, body: bytes | None = None) -> tuple[int, bytes]:
        if not self._api_key:
            raise SubtitleError("No OpenSubtitles API key yet - add one in Settings \u2192 Subtitles.")
        status, payload = self._transport(method, url, self._headers(post=body is not None), body, self._timeout)
        if status != 200:
            message = _STATUS_MESSAGES.get(status)
            if message is None:
                message = _error_message(payload) or f"OpenSubtitles returned HTTP {status}."
            raise SubtitleError(message, status)
        return status, payload

    # ------------------------------------------------------------------ search
    def search(
        self,
        query: str,
        languages: str = "en",
        *,
        movie_hash: str | None = None,
        movie_size: int | None = None,
        year: int | None = None,
        page: int = 1,
        per_page: int = 40,
    ) -> list[SubtitleResult]:
        params: dict[str, str] = {"page": str(page), "per_page": str(per_page)}
        codes = languages_param(languages)
        if codes:
            params["languages"] = codes
        if movie_hash and movie_size:
            params["moviehash"] = movie_hash
            params["moviebytesize"] = str(int(movie_size))
        elif query.strip():
            params["query"] = query.strip()
            if year:
                params["year"] = str(int(year))
        else:
            raise SubtitleError("Type something to search for.")

        url = f"{API_ROOT}/subtitles?{urllib.parse.urlencode(params)}"
        _, payload = self._call("GET", url)
        try:
            data = json.loads(payload or b"{}")
        except json.JSONDecodeError as error:
            raise SubtitleError("OpenSubtitles sent something that is not JSON.") from error
        return [_result_from(item) for item in data.get("data") or [] if _file_id(item)]

    # ---------------------------------------------------------------- download
    def download(self, file_id: int) -> tuple[bytes, str, int | None]:
        """Returns (file bytes, suggested name, downloads left today)."""
        body = json.dumps({"file_id": int(file_id), "sub_format": "srt"}).encode("utf-8")
        _, payload = self._call("POST", f"{API_ROOT}/download", body)
        try:
            data = json.loads(payload or b"{}")
        except json.JSONDecodeError as error:
            raise SubtitleError("OpenSubtitles sent something that is not JSON.") from error
        link = data.get("link")
        if not link:
            raise SubtitleError(data.get("message") or "OpenSubtitles did not return a download link.")
        remaining = data.get("remaining")
        status, content = self._transport("GET", link, {"User-Agent": USER_AGENT}, None, self._timeout)
        if status != 200 or not content:
            raise SubtitleError(_STATUS_MESSAGES.get(status, f"Download failed (HTTP {status})."), status)
        if content[:2] == b"\x1f\x8b":  # some older links are gzipped
            try:
                content = gzip.decompress(content)
            except OSError:
                pass
        return content, str(data.get("file_name") or ""), remaining if isinstance(remaining, int) else None

    def save(self, file_id: int, directory: str | Path, stem: str, language: str) -> Path:
        """Download and write as `<stem>.<lang>.srt`, never overwriting a file."""
        content, _, _ = self.download(file_id)
        folder = Path(directory)
        folder.mkdir(parents=True, exist_ok=True)
        suffix = f".{(language or 'sub').lower()}.srt"
        target = folder / f"{stem}{suffix}"
        counter = 2
        while target.exists():
            target = folder / f"{stem}{suffix[:-4]}.{counter}.srt"
            counter += 1
        try:
            target.write_bytes(content)
        except OSError as error:
            raise SubtitleError(f"Could not save the subtitle: {error.strerror or error}") from error
        return target


# ------------------------------------------------------------------- parsing
def _error_message(payload: bytes) -> str:
    try:
        data = json.loads(payload or b"{}")
    except json.JSONDecodeError:
        return ""
    if isinstance(data, dict):
        message = data.get("message") or data.get("error")
        return str(message) if message else ""
    return ""


def _file_id(item: dict) -> int | None:
    files = (item.get("attributes") or {}).get("files") or []
    if not files:
        return None
    try:
        return int(files[0].get("file_id"))
    except (TypeError, ValueError):
        return None


def _result_from(item: dict) -> SubtitleResult:
    attributes = item.get("attributes") or {}
    details = attributes.get("feature_details") or {}
    files = attributes.get("files") or []
    year_value = details.get("year")
    try:
        year = int(year_value) if year_value else None
    except (TypeError, ValueError):
        year = None
    rating_value = attributes.get("ratings")
    try:
        rating = float(rating_value) if rating_value else 0.0
    except (TypeError, ValueError):
        rating = 0.0
    return SubtitleResult(
        file_id=int(files[0].get("file_id")),
        language=str(attributes.get("language") or ""),
        title=str(details.get("title") or details.get("movie_name") or ""),
        year=year,
        release=str(attributes.get("release") or ""),
        downloads=int(attributes.get("download_count") or 0),
        rating=rating,
        hearing_impaired=bool(attributes.get("hearing_impaired")),
        filename=str(files[0].get("file_name") or attributes.get("filename") or ""),
    )


__all__ = [
    "API_KEY_SETTING",
    "API_ROOT",
    "OpenSubtitles",
    "SubtitleError",
    "SubtitleResult",
    "clean_query",
    "languages_param",
    "subtitle_hash",
]
