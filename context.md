# PROJECT: <Reel> — a modern, low-latency desktop media player

   ## Goal
   Build a media player that feels like VLC 3 (instant response, no jank, plays
   anything) but with a genuinely modern GUI. Reference feel: a YouTube-style player
   — video fills the window, controls float over it and fade out, dark UI.
   Target platform: Windows 10/11 x64. Personal project, not a product (yet).

   ## Non-negotiable requirements
   1. Playback latency and seek response must match or beat VLC 3. No UI thread
      work that can stall the video pipeline.
   2. Zero-copy video: frames go decoder -> GPU surface. Never decode to CPU
      memory and blit into Qt (that is what makes "extended VLC" clones janky).
   3. UI must stay responsive while a 4K file is playing and while seeking.
   4. Handles any file VLC handles (mkv/mp4/webm/hevc/av1/10-bit/subtitles/audio).

   ## Environment (already verified on this machine)
   - VLC 3.0.24 x64 installed at C:\Program Files\VideoLAN\VLC  (libvlc.dll there)
   - Python 3.12.9 + uv on PATH; git installed
   - Work on the Windows filesystem (e.g. C:\codes\<Reel>), NOT inside WSL.
     Rationale: WSLg runs Linux Qt and cannot load libvlc.dll, so the video
     embedding path cannot be tested from WSL at all.
   - Deps: PySide6 + python-vlc (in a uv venv). Do not bundle libVLC yet.

   ## Architecture decisions (already settled — do not relitigate)
   - Playback engine: libVLC via python-vlc. Only ONE module touches libvlc; it
     exposes plain Qt signals. UI never imports vlc.
   - Video output: libVLC paints into a native child HWND. Get it with
     `int(surface.winId())` on a QWidget with WA_NativeWindow, then
     `media_player.set_hwnd(hwnd)`.
   - Consequence: Windows composites native child windows ABOVE anything Qt paints
     in the same window. Therefore controls CANNOT be Qt widgets inside the main
     window. They live in a SEPARATE frameless, translucent, owned top-level window
     (Qt.Tool + WA_TranslucentBackground + WA_ShowWithoutActivating) that tracks the
     main window's client rect. This is how VLC 3's own Qt frontend does it.
   - UI stack: Qt Widgets + QSS tokens + custom QPainter painting for the seek bar,
     volume bar, and vector icons (no asset files, crisp at any DPI). Not QML —
     QML buys animated polish but adds a second language and scene-graph sync for
     no gain here, since the controls use a separate window either way.
   - Time updates: poll player time at ~30 Hz with a QTimer, do not rely on
     libvlc's coarse MediaPlayerTimeChanged events (for seek bar smoothness).
   - Keep libvlc options minimal: --intf=dummy --no-video-title-show --no-osd
     --quiet --file-caching=1000 --network-caching=1500. Disable VLC's own key and
     mouse input (video_set_key_input(False), video_set_mouse_input(False)).

   ## Milestone 1 scope (build only this first)
   A playable player: open file (dialog + drag&drop + CLI arg), play/pause, seek bar
   with hover time preview and drag, ±5s/±30s/±10s seeks, volume + mute, fullscreen
   (double-click, F, Esc), auto-hiding overlay controls, dark Windows title bar,
   keyboard shortcuts (Space/K, arrows, J/L, M, F, O, S, Ctrl+Q).
   Acceptance: open a 1080p mkv, scrub for 30s, no visible stutter, controls never
   lag the video, close cleanly.

   ## Explicitly NOT in M1
   Playlist, settings, subtitles/audio track menus, media library, frameless custom
   titlebar, packaging. Do not build these yet.

   ## Working rules
   - Never write files or run state-changing commands without asking me first.
     Propose a plan, wait for my go-ahead, then implement. Read-only inspection is
     fine anytime.
   - Never run git commit; give me the commands to run instead.
   - Small, reviewable changes. Explain trade-offs before big decisions..