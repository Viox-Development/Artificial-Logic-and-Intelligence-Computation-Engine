# Small extras

Written from the "Küçük eklemeler" section of the Turkish guide ([docs/README.tr.md](../README.tr.md)) and checked
against `alice/extras/`.

```powershell
alice notes add "look at the jump bug in Unity"     # or: "Alice, take a note…"
alice notes search jump 30d                          # or: "what did I note last week?"
alice notes undo                                     # delete the last note
alice brief                                          # the morning brief now
alice brief --install 07:45 --weekdays               # say it every weekday morning
alice report --open                                  # weekly computer report
alice report --install 20:00                         # every Sunday evening
```

- **Morning brief:** the weather, whether today is school or internship, the results of jobs that ran overnight,
  warnings from the night and notes left from yesterday. Put your city and schedule in the `[brief]` section of
  `config.toml`: `city = "Istanbul"`, `days = { pazartesi = "okul", salı = "staj" }` (day names in Turkish: Monday
  school, Tuesday internship).
- **Clipboard:** Ctrl+Alt+C in the quick bar.
- **Music and volume:** "Alice, next song", "set the volume to 30 percent", "stop the music in 20 minutes", "shut the
  computer down in an hour" ("cancel the shutdown" undoes it). Shutting down asks for approval. On Windows,
  `pip install pycaw` is recommended for exact volume; without it the volume keys set it approximately. Linux uses
  playerctl and pactl, macOS osascript.
