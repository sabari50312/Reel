"""Offer Reel in Windows' "Open with" list - per user, no admin, never silent.

Everything here writes under `HKEY_CURRENT_USER\\Software` (the `root` argument,
so a test can point the same code at `Software\\ReelTest` and delete it again):

  Software\\Classes\\Reel.Video                    ProgID for video files
        shell\\open\\command = "<exe>" "%1"
        DefaultIcon          = "<exe>",0
        FriendlyTypeName     = "Video file"
  Software\\Classes\\Reel.Audio                    the same, for audio
  Software\\Classes\\Applications\\Reel.exe        makes the exe show up in
        shell\\open\\command                          Explorer's "Choose another app"
        SupportedTypes\\<ext>                        one empty value per type
  Software\\Classes\\<ext>\\OpenWithProgids\\<progid>  adds Reel to the *candidate*
                                                    list for that extension
  Software\\Reel\\Capabilities                     metadata for the Default apps page
        FileAssociations\\<ext> = <progid>
  Software\\RegisteredApplications\\Reel           = "Software\\Reel\\Capabilities"

The `OpenWithProgids` route is the whole point: it is additive. It adds Reel to
the list you see in *Open with* without changing what a double click launches.
Nothing here writes `UserChoice` - Windows protects that key, and taking over a
file type behind the user's back is exactly what a portable app must not do.
Windows 10/11 only let an app become the default via Settings > Default apps,
which is why the settings card offers a button that opens that page.

Two things also live here because they are registry questions, not Qt ones:

  * `target_exe()` is None in a dev run (`python.exe main.py`). The settings row
    disables itself rather than registering the interpreter - that would make
    every Python script on the machine claim to open videos.
  * `refresh_if_moved()` repoints a stale registration when a portable folder has
    been moved, but only if the marker file says this *is* the portable build and
    only when the user had already opted in.
"""

from __future__ import annotations

import os
import sys
import winreg
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ROOT = r"Software"
# Keep in sync with reel.settings.PORTABLE_MARKER (that module imports Qt, this
# one deliberately does not, so the string is repeated rather than imported).
PORTABLE_MARKER = "reel.portable"

VIDEO = (
    ".mkv", ".mp4", ".m4v", ".mov", ".avi", ".wmv", ".webm",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".flv", ".ogv",
)
AUDIO = (".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus", ".wma", ".mka")

# (kind, extensions, ProgID, friendly name) - one row per ProgID we own.
KINDS: tuple[tuple[str, tuple[str, ...], str, str], ...] = (
    ("video", VIDEO, "Reel.Video", "Video file"),
    ("audio", AUDIO, "Reel.Audio", "Audio file"),
)

SHIPPED_EXE = "Reel.exe"  # what tools/build.py names the executable


@dataclass
class Registration:
    """What a `register()` call managed to write, and what it could not."""

    written: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


# --------------------------------------------------------------- basics
def is_frozen() -> bool:
    """True inside a packaged build.

    Checked via `__compiled__` because Nuitka does not set `sys.frozen`, and the
    distinction matters: a standalone build ships its own `python.exe` launcher
    and points `sys.executable` at that, so the executable that must be
    registered is the one in `argv[0]`, not `sys.executable`.
    """
    return "__compiled__" in globals() or bool(getattr(sys, "frozen", False))


def _exe_candidates() -> list[str]:
    """Paths worth trying, most trustworthy first.

    The order is not cosmetic: under Nuitka `sys.executable` is the bundled
    `python.exe`, which is exactly the path that must never be registered, so it
    goes last and the name check below rejects it. From source, `argv[0]` is
    `main.py` and only the interpreter is left - which the same check rejects,
    leaving nothing to register, as intended.
    """
    paths: list[str] = []
    compiled = globals().get("__compiled__")
    if compiled is not None:
        paths.append(str(getattr(compiled, "original_argv0", "") or ""))
    if sys.argv:
        argv0 = sys.argv[0]
        if Path(argv0).suffix.lower() == ".exe":
            paths.append(argv0)
    paths.append(sys.executable)
    return [path for path in paths if path]


def target_exe() -> Path | None:
    """The Reel executable to register, or None in a dev run."""
    for candidate in _exe_candidates():
        exe = Path(candidate)
        if exe.name.lower() in {"python.exe", "pythonw.exe"}:
            continue  # the interpreter (or Nuitka's launcher shim)
        try:
            resolved = exe.resolve()
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    return None


def extensions() -> tuple[str, ...]:
    return VIDEO + AUDIO


def is_portable(exe: Path | None = None) -> bool:
    exe = exe or target_exe()
    return exe is not None and (exe.parent / PORTABLE_MARKER).exists()


def open_default_apps() -> bool:
    """Windows' Settings > Default apps, the only place a default can be set."""
    starter = getattr(os, "startfile", None)
    if starter is None:
        return False
    try:
        starter("ms-settings:defaultapps")
    except OSError:
        return False
    return True


# --------------------------------------------------------------- registry io
def _path(root: str, *parts: str) -> str:
    return "\\".join((root, *parts))


def _write(path: str, name: str | None, value: str | bytes, kind: int = winreg.REG_SZ) -> None:
    # KEY_SET_VALUE alone cannot create the intermediate keys, so ask for both.
    access = winreg.KEY_SET_VALUE | winreg.KEY_CREATE_SUB_KEY
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, access) as key:
        winreg.SetValueEx(key, name or "", 0, kind, value)


def _read(path: str, name: str | None = None) -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            value, _kind = winreg.QueryValueEx(key, name or "")
    except OSError:
        return None
    return value if isinstance(value, str) else None


def _delete_value(path: str, name: str) -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except OSError:
        pass  # absent already, or the key belongs to something else


def _delete_tree(path: str) -> None:
    """Delete a key and everything under it, leaves first."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_ALL_ACCESS) as key:
            while True:
                try:
                    child = winreg.EnumKey(key, 0)
                except OSError:
                    break  # no subkeys left
                _delete_tree(path + "\\" + child)
            try:
                count = winreg.QueryInfoKey(key)[1]
            except OSError:
                count = 0
            for index in reversed(range(count)):
                try:
                    winreg.DeleteValue(key, winreg.EnumValue(key, index)[0])
                except OSError:
                    pass
    except FileNotFoundError:
        return
    except OSError:
        return  # locked by another process: nothing useful to do, and no exception
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except OSError:
        pass


def _exe_from_command(command: str | None) -> Path | None:
    """`"D:\\Apps\\Reel\\Reel.exe" "%1"` -> `D:\\Apps\\Reel\\Reel.exe`."""
    if not command:
        return None
    text = command.strip()
    if text.startswith('"'):
        end = text.find('"', 1)
        if end == -1:
            return None
        return Path(text[1:end])
    head = text.split(" ", 1)[0]  # never written unquoted, but be forgiving
    return Path(head) if head else None


def _same(first: Path, second: Path) -> bool:
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))


# --------------------------------------------------------------- public api
def registered_exe(*, root: str = DEFAULT_ROOT) -> Path | None:
    """The executable the current registration points at, if any."""
    return _exe_from_command(
        _read(_path(root, "Classes", "Reel.Video", "shell", "open", "command"))
    )


def is_registered(exe: Path | None = None, *, root: str = DEFAULT_ROOT) -> bool:
    """True when every key we own exists *and* points at this executable.

    The path comparison is what makes a moved portable folder read as "not
    registered" instead of showing a stale, broken entry in Explorer.
    """
    exe = exe or target_exe()
    if exe is None:
        return False
    stored = (
        registered_exe(root=root),
        _exe_from_command(_read(_path(root, "Classes", "Reel.Audio", "shell", "open", "command"))),
        _exe_from_command(
            _read(_path(root, "Classes", "Applications", exe.name, "shell", "open", "command"))
        ),
    )
    if any(item is None or not _same(item, exe) for item in stored):
        return False
    return _read(_path(root, "RegisteredApplications"), "Reel") == _path(root, "Reel", "Capabilities")


def register(exe: Path | None = None, *, root: str = DEFAULT_ROOT) -> Registration:
    """Write every key; collect failures instead of raising on the first one."""
    exe = (exe or target_exe())
    if exe is None:
        raise ValueError("no packaged executable to register")
    exe = Path(exe).resolve()
    command = f'"{exe}" "%1"'
    icon = f'"{exe}",0'
    result = Registration()

    def put(sub: str, name: str | None, value: str | bytes, kind: int = winreg.REG_SZ) -> None:
        full = _path(root, sub)
        try:
            _write(full, name, value, kind)
        except OSError as error:
            result.failed.append(f"{full}\\{name or '(default)'}: {error}")
        else:
            result.written.append(f"{full}\\{name}" if name else full)

    for _kind, exts, progid, label in KINDS:
        # shell\open\command is a *key* whose default value is the command line.
        put(f"Classes\\{progid}\\shell\\open\\command", None, command)
        put(f"Classes\\{progid}", "DefaultIcon", icon)
        put(f"Classes\\{progid}", "FriendlyTypeName", label)
        for ext in exts:
            # A zero-length value: Explorer only looks at the *name* here.
            put(f"Classes\\{ext}\\OpenWithProgids", progid, b"", winreg.REG_NONE)

    application = f"Classes\\Applications\\{exe.name}"
    put(f"{application}\\shell\\open\\command", None, command)
    for ext in extensions():
        put(f"{application}\\SupportedTypes", ext, "")

    capabilities = "Reel\\Capabilities"
    put(capabilities, "ApplicationName", "Reel")
    put(capabilities, "ApplicationDescription", "Reel media player")
    put(capabilities, "ApplicationIcon", icon)
    for _kind, exts, progid, _label in KINDS:
        for ext in exts:
            put(f"{capabilities}\\FileAssociations", ext, progid)
    put("RegisteredApplications", "Reel", _path(root, "Reel", "Capabilities"))

    return result


def unregister(exe: Path | None = None, *, root: str = DEFAULT_ROOT) -> None:
    """Remove everything `register` wrote; leave every other player alone."""
    names = {SHIPPED_EXE}
    for candidate in (registered_exe(root=root), exe or target_exe()):
        if candidate is not None:
            names.add(candidate.name)

    for _kind, exts, progid, _label in KINDS:
        _delete_tree(_path(root, "Classes", progid))
        for ext in exts:
            _delete_value(_path(root, "Classes", ext, "OpenWithProgids"), progid)
    for name in names:
        _delete_tree(_path(root, "Classes", "Applications", name))
    _delete_tree(_path(root, "Reel"))
    _delete_value(_path(root, "RegisteredApplications"), "Reel")


def refresh_if_moved(*, root: str = DEFAULT_ROOT) -> bool:
    """Repoint a stale registration after the portable folder was moved.

    Only for the portable build, and only when the user had already turned the
    toggle on: the marker says this copy is *meant* to be moved around, and an
    existing registration is their opt-in.
    """
    exe = target_exe()
    if exe is None or not is_portable(exe):
        return False
    stored = registered_exe(root=root)
    if stored is None or _same(stored, exe):
        return False
    return register(exe, root=root).ok


__all__ = [
    "AUDIO",
    "KINDS",
    "PORTABLE_MARKER",
    "Registration",
    "VIDEO",
    "extensions",
    "is_portable",
    "is_registered",
    "open_default_apps",
    "refresh_if_moved",
    "register",
    "registered_exe",
    "target_exe",
    "unregister",
]
