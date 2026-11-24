# Quick bar

Written from the "Hızlı bar" section of the Turkish guide ([docs/README.tr.md](../README.tr.md)) and checked against
`alice/app/quick.py`, `alice/app/static/quick.js` and the `[quick]` defaults in `alice/config.py`.

Whatever program you are in:

- **Ctrl+Alt+A:** a small ALICE box opens in the middle of the screen. Type, or hold the microphone button (or
  Ctrl+Space) and speak.
- **Ctrl+Alt+S:** the screen dims and you select an area with the mouse (an error message, the Unity console...). The
  selected area is added to your question.
- **Ctrl+Alt+C:** explains the error you copied to the clipboard.

It starts with the core (`[quick] enabled = true`); to run it on its own, use `alice quick`. The shortcuts are in the
`[quick]` section (`hotkey`, `region_hotkey`, `clipboard_hotkey`). If it isn't used for 10 minutes, the next time it
opens with a new conversation. Esc closes it; while ALICE is working, Esc stops the job.

On Linux the shortcuts work on X11; Wayland doesn't allow global shortcuts, and ALICE says so (#239).
