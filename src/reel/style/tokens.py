"""Design tokens — the Qt mirror of mockup/ui.html.

Colors are Qt hex strings (#RRGGBB or #AARRGGBB) so the same value works in QSS
and in QColor(...) for the hand-painted widgets. Nothing in the codebase should
hardcode a color, a padding or an animation duration: add a token here instead.
"""

# ---------------------------------------------------------------- palette ----
BG = "#0b0d10"  # window fill outside the video
VIDEO_BG = "#000000"  # letterbox fill behind the native HWND
TEXT = "#f2f4f7"
TEXT_DIM = "#9aa4af"
TEXT_FAINT = "#6b7683"

ACCENT = "#f2f4f7"  # played portion + knob (white)
TRACK = "#42ffffff"  # seek/volume track at rest      (white @ 26%)
TRACK_HOVER = "#61ffffff"  # track under the cursor      (white @ 38%)
BUFFER = "#24ffffff"  # buffered section              (white @ 14%)
BTN_HOVER = "#1fffffff"  # control button hover         (white @ 12%)
BTN_ACTIVE = "#2effffff"  # control button pressed       (white @ 18%)
SCRIM = "#e6000000"  # bottom gradient end           (black @ 90%: text legibility)
SCRIM_START = "#00000000"
SCRIM_MID = "#8c000000"  # black @ 55%
TITLEBAR = "#101317"
TOAST_BG = "#d1000000"

# ---------------------------------------------------------------- metrics ----
SEEK_H = 4
SEEK_H_HOVER = 6
SEEK_KNOB = 12
SEEK_HIT = 26  # vertical hit area of the seek row
BUBBLE_H = 22  # hover time bubble drawn above the seek row
VOL_W = 76  # full width of the volume slider
ICON = 24
BTN = 40
PAD = 16  # control bar side padding
GAP = 12
RADIUS_CTL = 4
SCRIM_RATIO = 0.38  # scrim height as a fraction of the player height
OVERLAY_H = 112  # height of the overlay strip at the bottom of the player

FS_TIME = 13
FS_TITLE = 12
FONT_FAMILY = "Segoe UI"

FADE_MS = 180  # overlay show/hide
GROW_MS = 120  # seek track height
HIDE_MS = 2600  # idle delay before the overlay hides
FLASH_IN_MS = 90  # persistent feedback: fade in
FLASH_HOLD_MS = 500  # persistent feedback: time at full opacity
FLASH_OUT_MS = 180  # persistent feedback: fade out
FLASH_DISC = 88  # diameter of the centre icon disc
PILL_W = 56  # right-hand volume pill
PILL_H = 300


# ---- values exposed to tokens.qss via string.Template ----------------------
QSS_VARS = {
    "BG": BG,
    "VIDEO_BG": VIDEO_BG,
    "TEXT": TEXT,
    "TEXT_DIM": TEXT_DIM,
    "TEXT_FAINT": TEXT_FAINT,
    "ACCENT": ACCENT,
    "TRACK": TRACK,
    "TRACK_HOVER": TRACK_HOVER,
    "BTN_HOVER": BTN_HOVER,
    "BTN_ACTIVE": BTN_ACTIVE,
    "TITLEBAR": TITLEBAR,
    "RADIUS_CTL": str(RADIUS_CTL),
    "FS_TIME": str(FS_TIME),
    "FS_TITLE": str(FS_TITLE),
    "FONT_FAMILY": FONT_FAMILY,
}
