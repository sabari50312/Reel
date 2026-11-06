"""Build the portable Reel folder.

Nuitka produces `dist/Reel/Reel.exe` plus the Python/Qt dependency folder; this
script then makes that folder self-contained:

  1. copies libVLC and the plugins it actually loads into `vlc/`,
  2. prunes the encoder DLLs PyAV ships (Reel only decodes audio for the sound
     map, and those encoders are also the GPL parts of the bundle),
  3. drops a `reel.portable` marker so settings land in `reel.ini` beside the exe
     instead of the registry,
  4. zips the result.

Run:  uv run python tools/build.py [--skip-nuitka] [--no-zip]
Nothing here is imported by the app.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
TARGET = DIST / "Reel"
VERSION = "0.2.0"

# VLC parts that a headless player never loads: the Qt interface (we start with
# --intf=dummy), UI translations, skins and lua scripts.
VLC_SKIP_PLUGINS = {"gui", "lua", "skins"}
VLC_TOP_LEVEL = {"libvlc.dll", "libvlccore.dll", "AUTHORS", "COPYING", "COPYING.LIB", "NEWS", "README.txt"}

# PyAV's FFmpeg DLLs cannot be pruned: avcodec links the codec libraries directly,
# so removing libx264/libx265 makes `av._core` fail to load at all (measured).

NUITKA_ARGS = [
    "--standalone",
    "--enable-plugin=pyside6",
    "--windows-console-mode=disable",
    "--assume-yes-for-downloads",
    "--company-name=Sabar",
    "--product-name=Reel",
    "--file-description=Reel media player",
    f"--file-version={VERSION}",
    f"--product-version={VERSION}",
    "--copyright=Personal project",
    # Data files the app reads at runtime: Nuitka only follows imports.
    "--include-data-files=src/reel/style/tokens.qss=reel/style/tokens.qss",
    # The wordmark font travels with the app (Sora, SIL OFL - see OFL.txt).
    "--include-data-dir=src/reel/assets=reel/assets",
    # PyAV imports a few Cython helpers by name, which Nuitka cannot see from the
    # source alone; without them `import av` fails with "No module named 'av._cyutil'".
    "--include-package=av",
    "--output-dir=dist",
    "--output-filename=Reel.exe",
    "main.py",
]


def vlc_source() -> Path | None:
    for candidate in (ROOT / "vlc", Path(r"C:\Program Files\VideoLAN\VLC")):
        if (candidate / "libvlc.dll").is_file():
            return candidate
    return None


def copy_vlc() -> int:
    """Copy libVLC + usable plugins into dist/Reel/vlc. Returns bytes copied."""
    source = vlc_source()
    if source is None:
        print("! libVLC not found: the build will need an installed VLC")
        return 0
    target = TARGET / "vlc"
    target.mkdir(parents=True, exist_ok=True)
    copied = 0
    for item in source.iterdir():
        if item.is_file() and (item.name in VLC_TOP_LEVEL or item.suffix == ".dll"):
            shutil.copy2(item, target / item.name)
            copied += item.stat().st_size
    plugins_target = target / "plugins"
    for folder in sorted((source / "plugins").iterdir()):
        if not folder.is_dir() or folder.name in VLC_SKIP_PLUGINS:
            continue
        shutil.copytree(folder, plugins_target / folder.name, dirs_exist_ok=True)
        copied += sum(p.stat().st_size for p in (plugins_target / folder.name).rglob("*") if p.is_file())
    print(f"  vlc: {copied / 1e6:.0f} MB from {source}")
    return copied


def write_marker() -> None:
    (TARGET / "reel.portable").write_text("portable\n", encoding="utf-8")


def zip_folder() -> Path:
    archive = DIST / f"Reel-{VERSION}-win64.zip"
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=7) as bundle:
        for path in sorted(TARGET.rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(DIST))
    print(f"  zip: {archive.name} {archive.stat().st_size / 1e6:.0f} MB")
    return archive


def folder_size() -> float:
    return sum(p.stat().st_size for p in TARGET.rglob("*") if p.is_file()) / 1e6


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-nuitka", action="store_true", help="repackage an existing dist/Reel")
    parser.add_argument("--no-zip", action="store_true")
    args = parser.parse_args()

    if not args.skip_nuitka:
        print("nuitka: compiling (5-15 minutes on the first run)")
        subprocess.run([sys.executable, "-m", "nuitka", *NUITKA_ARGS], cwd=ROOT, check=True)

    # Nuitka names the output folder after the entry module (`main.dist`); the
    # shipped folder should read as the app, so move it into place. A rebuild must
    # replace the previous folder - Nuitka writes to `main.dist` every time, so
    # leaving the old `Reel/` in place would ship the previous executable.
    built = DIST / "main.dist"
    if built.is_dir():
        # Keep the test run's settings: they describe what the user was doing when
        # they found the bug, and rebuilding is not a reason to lose them.
        carried: dict[str, bytes] = {}
        if TARGET.exists():
            for name in ("reel.ini",):
                previous = TARGET / name
                if previous.is_file():
                    carried[name] = previous.read_bytes()
            shutil.rmtree(TARGET)
        shutil.move(str(built), str(TARGET))
        for name, data in carried.items():
            (TARGET / name).write_bytes(data)
        print(f"  renamed {built.name} -> {TARGET.name}" + (" (kept reel.ini)" if carried else ""))

    if not (TARGET / "Reel.exe").is_file():
        print(f"! {TARGET / 'Reel.exe'} missing - run without --skip-nuitka first")
        return 1

    copy_vlc()
    write_marker()
    if not args.no_zip:
        zip_folder()
    print(f"done: {TARGET} ({folder_size():.0f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
