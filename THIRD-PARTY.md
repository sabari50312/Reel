# Third-party notices

Reel's own code is MIT (see `LICENSE`). The packaged build also contains the
components below, each under its own licence. Full licence texts travel with the
build in `dist/Reel/third_party/`, and are in this folder in the repo.

## libVLC — LGPL-2.1-or-later

The playback engine. A packaged Reel ships VLC's unmodified `libvlc.dll`,
`libvlccore.dll`, `axvlc.dll` and the plugin folders in `vlc/` next to the exe.

- Copyright © the VideoLAN project.
- Source: <https://code.videolan.org/videolan/vlc>
- Licence text: `third_party/LGPL-2.1.txt`

Because the DLLs are shipped as separate files (not linked in), you can rebuild or
swap libVLC for your own LGPL-compatible build: replace the contents of `vlc/`
and Reel will use it. `REEL_VLC_DIR` points Reel at a different VLC folder
entirely.

## python-vlc — LGPL-2.1-or-later

The Python binding Reel uses to call libVLC. Only `reel/engine.py` imports it.

- Source: <https://github.com/oaubert/python-vlc>
- Licence text: `third_party/LGPL-2.1.txt`

## PySide6 / Qt 6 — LGPL-3.0

The GUI toolkit. Shipped as Qt DLLs and Python extension modules in the build.

- Copyright © The Qt Company.
- Source: <https://code.qt.io/cgit/pyside/pyside-setup.git>
- Licence text: `third_party/LGPL-3.0.txt`

The Qt libraries are loaded from disk at runtime, so they can be replaced with
your own LGPL-compatible build of the same version.

## PyAV / FFmpeg — BSD-3-Clause / LGPL-2.1-or-later

Used for the audio loudness map on the seek bar (demux + decode to PCM). The
bundled FFmpeg libraries are an LGPL build.

- PyAV: <https://github.com/PyAV-Org/PyAV> (BSD-3-Clause)
- FFmpeg: <https://ffmpeg.org> (LGPL-2.1-or-later)
- Licence text: `third_party/LGPL-2.1.txt`

## Inter — SIL Open Font License 1.1

The UI typeface, instanced from the variable font at the text optical size and
shipped as `reel/assets/fonts/Inter-{Regular,Medium,SemiBold}.ttf`.

- Copyright 2020 The Inter Project Authors (<https://github.com/rsms/inter>)
- Licence text: `third_party/OFL-1.1.txt`, plus the font's own header in
  `reel/assets/fonts/OFL-Inter.txt`

## Sora — SIL Open Font License 1.1

The wordmark face on the home screen, shipped as `reel/assets/fonts/Sora-SemiBold.ttf`.

- Copyright 2019 The Sora Project Authors (<https://github.com/sora-xor/sora-font>)
- Licence text: `reel/assets/fonts/OFL.txt`

## Icon set

The UI icons in `icons/` are Google Material Symbols outlines, used as reference
geometry for the hand-painted vector icons in `reel/widgets/icons.py`
(Apache License 2.0, <https://github.com/google/material-design-icons>). The app
icon is drawn from scratch by `tools/make_icon.py`.
