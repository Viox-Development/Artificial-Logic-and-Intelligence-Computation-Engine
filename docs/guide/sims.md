# DCS and War Thunder

Written from the "DCS ve War Thunder" section of the Turkish guide ([docs/README.tr.md](../README.tr.md)) and checked
against `alice/sims/`. Spoken calls are in Turkish; examples are given with their meaning.

ALICE never sends anything to the game; it only reads and talks. It doesn't repeat the F-16's own voice warnings; it
acts like a wingman.

```powershell
alice dcs install        # puts a read-only script in Saved Games\DCS\Scripts and adds one line to Export.lua
alice core run           # flight data is processed while the core runs (already on with alice core install)
alice dcs status
alice dcs uninstall      # undo
```

The script works alongside other export scripts such as SRS, TacView and Helios. It sends the data 10 times a second
over local UDP (port 41230).

- **Fuel:** not just "bingo". It says how many minutes are left at the current burn, the distance to base and within
  how many minutes you must leave to make it home. After air-to-air refueling the warnings are set up again.
- **Approach:** with the gear down, low and slow, it calls "fast", "slow, power" or "on speed" by AoA, and warns if
  the sink rate is too high. When you are doing it right it stays quiet.
- **Reminders:** it tells you if you stay at high G for long, or when chaff or flares run low or out.
- **Checklists:** say "Alice, landing checklist" and it reads it step by step, moving on each time you say "tamam"
  (OK). Simplified F-16C lists ship with ALICE. Lists you put in `~/.alice/checklists/*.md` are used instead (the
  heading is the list's name, each `- ` line is a step).
- **Kneeboard:** "Alice, prepare the kneeboard" puts the flight's frequency, the waypoints (altitude, distance,
  bearing) and the briefing from the mission file on the in-game kneeboard as pages. In flight, "Alice, take a note:
  tanker 251.0" adds it to the notes page. By hand: `alice dcs kneeboard [mission.miz]`.
- **After the flight:** after landing it gives a short spoken debrief: duration, peak G, fuel, the on-speed share of
  the approach and how hard the touchdown was. It writes a report with charts to `~/.alice/flights/`. With watch data
  the heart rate goes into the report too; when your heart rate is high in flight, it gives warnings in a calm but
  clear tone instead of an urgent one.
- **Multiplayer:** if the server restricts data export, ALICE says so. Checklists, the kneeboard and voice commands
  keep working.

Supported profiles: F-16C (on-speed AoA 11-15°) and F/A-18C (7.4-8.8°). Other aircraft use general values. The
profiles are in `alice/sims/flight.py`.

**War Thunder:** while the game is open, ALICE reads its local indicators (`http://127.0.0.1:8111`). Fuel, G and
approach warnings and the post-flight debrief work the same way. No setup needed.
