# Testing

The user does the UI/UX testing for this project. It is faster and more
accurate than anything I can automate, so don't duplicate it.

- Do NOT launch the app, take screenshots, record the desktop, or drive the
  mouse/window APIs to check how something looks, feels, or animates.
- Do NOT add ffmpeg/gdigrab/ddagrab capture helpers or screenshot probes.
- Instead: finish the change, then hand over a short numbered checklist of what
  to click and what to expect, and wait for the results.
- Cheap non-visual checks are fine: imports, `py_compile`, reading code,
  unit-level logic. If a bug can only be reproduced by running the app, say why
  and ask before running it.

# Git

- Never run `git commit`. Give the user the commands to run instead.
