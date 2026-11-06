# Reel

A Windows media player built on libVLC. Plays what VLC plays, looks nothing like it.

Reel is a single-file player for Windows: video fills the window, the controls
fade over it, and nothing else is on screen. mkv, mp4, webm, HEVC, AV1, 10-bit,
multiple audio and subtitle tracks — decoded by libVLC and drawn straight to the
window, so seeking and 4K playback stay smooth.

The interface is drawn entirely in code — vector icons, painted seek bar, no image
assets and no QML layer.

Version 0.0.1, Windows 10/11 x64.

## Features

- Hardware-decoded libVLC playback into a native surface, with no CPU frame copy.
- Auto-hiding overlay: seek bar with hover preview and a loudness map of the file,
  so quiet and loud stretches are visible before you scrub.
- Subtitle tracks with live delay nudging, a sync strip that plots caption cues
  against that loudness map, and styling that applies without reopening the file.
- OpenSubtitles search, hash-matched to the file, using your own API key.
- File browser with quick places, drives, history and a path bar you click to edit.
- Per-file resume, single instance, drag and drop, portable mode, dark title bar.
- Opt-in *Open with* registration for 13 video and 9 audio formats, per user, never
  taking over the default.

## Requirements

- Windows 10 or 11, 64-bit.
- To run from source: Python 3.12, [uv](https://docs.astral.sh/uv/), and a 64-bit
  VLC 3 installation. Release builds carry their own VLC.

## Run from source

```powershell
uv sync
uv run python main.py "C:\path\to\video.mkv"
```

The path is optional. Reel looks for `libvlc.dll` beside the executable, in a
`vlc\` folder, then in `C:\Program Files\VideoLAN\VLC`; set `REEL_VLC_DIR` to use
another installation.

## Build

```powershell
uv run python tools/build.py
```

Nuitka compiles the app, then the script copies VLC and the licence texts into
`dist\Reel\`, drops the portable marker, and writes
`dist\Reel-<version>-win64.zip` (~121 MB; the folder itself is ~293 MB, mostly
VLC). `--skip-nuitka` repackages an existing build, `--no-zip` skips the archive.
Building requires VLC on the build machine.

## Settings

Stored per user in `HKCU\Software\Reel\Reel`. Portable builds keep `reel.ini`
beside the executable instead, because the build places a `reel.portable` marker
there. Key bindings are changed in Settings → Keybindings by pressing the keys you
want. Unhandled errors are appended to `%LOCALAPPDATA%\Reel\reel.log`.

## How it is built

libVLC paints into a native child window inside the Qt window, and Windows
composites native children above anything Qt draws in that window — so a widget
placed over the video would be invisible. The controls therefore live in a
separate frameless, translucent, always-on-top window that tracks the player's
client rect, which is also how VLC's own Qt frontend solves it. Playback time is
polled at ~30 Hz rather than taken from libVLC's coarse time events, and only
`reel/engine.py` imports `vlc`; everything above it is Qt signals.

## Limitations

- Windows only. The video embedding path has no Linux or macOS equivalent.
- The executable is unsigned, so SmartScreen warns on first run: *More info* →
  *Run anyway*.
- No installer. Unzip and run; deleting the folder uninstalls, though settings stay
  in the registry until you remove `HKCU\Software\Reel`.
- One file at a time — no playlist, no library, no codec picker.
- Online subtitles need a free OpenSubtitles API key.

## Licence

MIT, see `LICENSE`. The build also contains libVLC (LGPL-2.1), Qt and PySide6
(LGPL-3.0), FFmpeg (LGPL-2.1) and the Inter and Sora typefaces (OFL-1.1); see
`THIRD-PARTY.md` for the details and the bundled licence texts.
