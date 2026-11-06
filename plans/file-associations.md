# Plan: file associations

Goal: after a portable copy of Reel runs once (or when the user turns the toggle
on), double-clicking a video/audio file offers Reel in the Windows "Open with"
list, and Reel can be picked as the default player without admin rights.

Constraints: portable folder that may be moved, per-user only (no UAC), never
silently steal the default handler, and never touch associations in the dev
build.

---

## 1. What gets written

All under `HKCU\Software\Classes` — per user, no elevation, removed by deleting
the same keys.

| Key | Purpose |
| --- | --- |
| `Reel.Video\shell\open\command` | `"<exe>" "%1"` |
| `Reel.Video\DefaultIcon` | `"<exe>",0` (needs the icon task first; until then Windows shows a generic one) |
| `Reel.Video\FriendlyTypeName` | "Video file" |
| `Reel.Audio\...` | same three, "Audio file" |
| `Applications\Reel.exe\shell\open\command` | makes the exe appear under "Open with" and in Explorer's *Choose another app* |
| `Applications\Reel.exe\SupportedTypes` | one value per extension, empty string |
| `.mkv`, `.mp4`, ... `\OpenWithProgids` | `Reel.Video` = empty value — adds Reel to the *candidate* list **without** changing the current default |
| `Software\Reel\Capabilities` | `ApplicationName`, `ApplicationDescription`, `FileAssociations` (ext → ProgID) |
| `Software\Reel\RegisteredApplications` | `Reel` = `Software\Reel\Capabilities` — lets Windows' *Default apps* page list Reel |

The `OpenWithProgids` route is the important one: it is additive and reversible.
Nothing in this plan writes to `UserChoice`, which Windows protects and which
would make Reel the default behind the user's back.

Extensions to claim (one table, filterable later):

```
video: .mkv .mp4 .m4v .mov .avi .wmv .webm .mpg .mpeg .ts .m2ts .flv .ogv
audio: .mp3 .m4a .aac .flac .wav .ogg .opus .wma .mka
```

## 2. Where it lives in the app

New module `src/reel/associations.py`, no Qt imports:

```python
EXTENSIONS = {"video": [...], "audio": [...]}
def register(exe: Path) -> list[str]      # returns keys written
def unregister() -> None                  # deletes Reel.Video / Reel.Audio /
                                          # Reel.exe / Capabilities / RegisteredApplications
def is_registered() -> bool               # everything present and pointing at this exe
def target_exe() -> Path | None           # sys.executable when frozen, None in dev
```

- Use `winreg` with `KEY_SET_VALUE | KEY_CREATE_SUB_KEY`; every write is
  `try/except OSError` and collected, so a locked key degrades to a message
  rather than an exception.
- `is_registered()` compares the stored command path against `target_exe()`, so a
  moved portable folder reads as "not registered" and offers to re-register.
- Dev builds (`target_exe() is None`) hide the whole section: never register the
  venv's `python.exe main.py`.

## 3. Settings surface

`settings_panel.py`, Interface tab, new **Windows integration** card:

- Switch row **"Add Reel to the Open with menu"** → `register()` / `unregister()`.
  Default off; the row is disabled when `target_exe()` is None with a caption
  "Available in the packaged app".
- After registering, a caption under the switch: *"Open with → Choose another
  app → Reel. Windows stores your default per file type, so pick it once per
  extension."* Honest about what happened — a toggle that claims to be the
  default player would be a lie.
- A **"Become the default player"** button is deliberately *not* included:
  Windows 10/11 only allow that from the Settings app
  (`ms-settings:defaultapps`). Offer a link that opens that page instead.
- The toggle stores nothing in `settings.py`; the registry *is* the state, read on
  panel open (same pattern as the analysis cache).

## 4. Portable behaviour

- The registered command embeds the absolute exe path. A moved folder therefore
  points at nothing — hence `is_registered()`'s path comparison and a one-line
  notice in the card: "Registered from `D:\Apps\Reel`. Moved the folder? Toggle
  off and on again."
- On start-up with a `reel.portable` marker: if `is_registered()` is True but the
  path no longer matches, silently re-register (cheap: five keys), because the
  user already opted in and only the path changed.
- Uninstall = delete the folder; the stale registry keys are harmless and the
  toggle exists to clean them.

## 5. Second launch / "Open with" flow

Already built in `src/reel/single_instance.py`: a second launch forwards the file
to the running window and exits. Nothing new needed here, but verify the argument
arrives when Explorer launches the exe with a path containing spaces.

## 6. Order of work

1. `associations.py` + a headless test that registers into a scratch root
   (`HKCU\Software\Classes` is not mockable in winreg — add an optional `root`
   argument defaulting to `Software\Classes` so tests can pass
   `Software\ReelTest\Classes` and delete it afterwards).
2. Settings card + wiring; disable in dev.
3. Re-register-on-move logic in `main.py`.
4. Icon: `DefaultIcon` is only convincing once the app has its own icon, so the
   association work lands first and the icon task then only changes one value.
5. Rebuild with `tools/build.py`, then hand over the Explorer checks.

## 7. Manual checks (user)

1. Toggle **on**, right-click a `.mkv` → *Open with* shows Reel.
2. Double-click that file while Reel runs with something else → the window
   switches to the new film, no second window, no error.
3. Toggle **off**, re-check *Open with* — Reel is gone, the previous default is
   untouched.
4. Move `dist\Reel` to another folder, run it → the card says "Registered from
   <old path>" until the toggle is cycled, and the auto re-register on start-up
   fixes the command path.
5. `ms-settings:defaultapps` → Reel appears in the list.

## Out of scope

- Making Reel the default automatically (needs `UserChoice`, blocked by design).
- Explorer thumbnail/preview handlers (real registered COM servers — later, and
  only if the icon/preview work happens).
- Machine-wide install (`HKLM`, needs admin).
- Context-menu entries ("Play with Reel", "Add to Reel queue").
