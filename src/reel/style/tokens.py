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

ACCENT = "#f2f4f7"  # knob and primary button (white)
ACCENT_GREEN = "#35d07f"  # app accent: logo, played audio, seek-bar blocks
BLOCK_OFF = "#2b2f34"  # seek-bar rail: dark, so the sound blocks read as highlights
BLOCK_ON = "#5b6470"  # a slot with sound, ahead of the playhead
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

# The floating bar is its own card now (the reference has no gradient scrim).
HOME_TOP = "#22262b"  # empty-state surface: Material dark container, lifted off black
BAR_BG = "#f00a0b0e"  # control bar fill                 (near-black @ 94%)
BAR_BORDER = "#14ffffff"  # card hairline                    (white @ 8%)
MENU_BG = "#f70d0f12"  # popup background                (near-black @ 97%)
MENU_BORDER = "#1affffff"
SEP = "#18ffffff"  # dividers inside popups           (white @ 9%)
ON_ACCENT = "#0b0d10"  # glyph color on an accent-filled button

# ---------------------------------------------------------------- metrics ----
SEEK_H = 18  # chunky rail: the sound highlights need room to read
SEEK_H_HOVER = 20
SEEK_KNOB = 13
SEEK_HIT = 26  # vertical hit area of the seek row
BUBBLE_H = 22  # hover time bubble drawn above the seek row
SOUND_H = 14  # base block height when the loudness map is drawn
WAVE_H = 20  # tallest block: loud passages rise above the rail
VOL_W = 76  # full width of the volume slider
ICON = 24
BTN = 40
PAD = 16  # control bar side padding
GAP = 12
RADIUS_CTL = 4
SCRIM_RATIO = 0.38  # scrim height as a fraction of the player height
OVERLAY_H = 84  # strip the overlay reserves: BAR_H + top/bottom margin

FS_TIME = 13
FS_TITLE = 12
FS_SMALL = 11  # menu helper text, tooltips
FONT_FAMILY = "Segoe UI"
FONT_MONO = "Consolas"  # tabular timecodes: digits do not jitter while playing

BAR_H = 72  # floating control card height (everything in one row)
BAR_RADIUS = 14
BTN_SM = 36  # secondary transport / utility buttons
PLAY_W = 50  # play button is a wide primary chip, not a square icon
PLAY_H = 36
BAR_EDGE = 20  # gap between the card and the window edges (also raises it)
PANEL_BG = "#17181b"  # panel surface: near-black, fields sit *lighter* inside it
PANEL_BORDER = "#2a2c31"
PANEL_FIELD = "#101113"  # value boxes inside a panel: the reference's darkest element
PANEL_FIELD_BORDER = "#2a2c31"
PANEL_DIVIDER = "#24262b"  # hairline between settings sections
TEXT_LABEL = "#8b929c"  # row labels: dimmer than $TEXT, brighter than $TEXT_FAINT
PANEL_W = 344
SIDEBAR_W = 440  # settings sidebar: three compact labelled tabs + close button  # side file browser
PANEL_RADIUS = 14
ROW_H = 38  # one entry in a side panel list
MENU_ITEM_H = 34  # reference uses ~52px rows; 34 keeps 20 entries usable

FADE_MS = 180  # overlay show/hide
GROW_MS = 120  # seek track height
HIDE_MS = 2600  # idle delay before the overlay hides
FLASH_IN_MS = 90  # persistent feedback: fade in
FLASH_HOLD_MS = 500  # persistent feedback: time at full opacity
FLASH_OUT_MS = 180  # persistent feedback: fade out
FLASH_DISC = 88  # diameter of the centre icon disc
PILL_W = 96  # right-hand volume readout (no container)
PILL_H = 460
PILL_RADIUS = 26  # taller capsule: a corner, not a pill


# ---- values exposed to tokens.qss via string.Template ----------------------
QSS_VARS = {
    "BG": BG,
    "VIDEO_BG": VIDEO_BG,
    "TEXT": TEXT,
    "TEXT_DIM": TEXT_DIM,
    "TEXT_FAINT": TEXT_FAINT,
    "ACCENT": ACCENT,
    "ACCENT_GREEN": ACCENT_GREEN,
    "BLOCK_OFF": BLOCK_OFF,
    "BLOCK_ON": BLOCK_ON,
    "PANEL_BG": PANEL_BG,
    "PANEL_BORDER": PANEL_BORDER,
    "PANEL_FIELD": PANEL_FIELD,
    "PANEL_FIELD_BORDER": PANEL_FIELD_BORDER,
    "PANEL_DIVIDER": PANEL_DIVIDER,
    "TEXT_LABEL": TEXT_LABEL,
    "TRACK": TRACK,
    "TRACK_HOVER": TRACK_HOVER,
    "BTN_HOVER": BTN_HOVER,
    "BTN_ACTIVE": BTN_ACTIVE,
    "TITLEBAR": TITLEBAR,
    "BAR_BG": BAR_BG,
    "BAR_BORDER": BAR_BORDER,
    "MENU_BG": MENU_BG,
    "MENU_BORDER": MENU_BORDER,
    "SEP": SEP,
    "FS_SMALL": str(FS_SMALL),
    "WEIGHT_SEMIBOLD": "600",
    "RADIUS_CTL": str(RADIUS_CTL),
    "FS_TIME": str(FS_TIME),
    "FS_TITLE": str(FS_TITLE),
    "FONT_FAMILY": FONT_FAMILY,
    "FONT_MONO": FONT_MONO,
}
