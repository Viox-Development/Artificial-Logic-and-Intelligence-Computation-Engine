# Background core

Written from the "Arka plan çekirdeği" section of the Turkish guide ([docs/README.tr.md](../README.tr.md)) and checked
against `alice/core/` and the `[core]` defaults in `alice/config.py`.

Under ALICE runs a small service that keeps working when you are not typing. It watches the computer, tells you about
problems that last, switches modes to fit what you are doing and runs scheduled jobs.

```powershell
pip install -e ".[core]"     # for the system tray icon (optional)
alice core install           # start quietly in the tray when Windows starts (undo: alice core uninstall)
alice core run --tray        # start now (or just: alice core run; --once: measure one round and exit)
alice core status            # is it running, which mode, recent notifications
alice core mode flight       # lock a mode by hand (normal, dev, flight, quiet); alice core mode auto returns to automatic
alice core stats "pc.gpu.*" 1d
```

## Data

Every 15 seconds (`[core] interval`) the CPU, RAM, disks, the programs using the most resources, the foreground
window and, on NVIDIA cards, GPU temperature and load are written to `~/.alice/core.db` and kept for 30 days
(`retention_days`). On Windows, CPU temperature needs
[LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) running (psutil doesn't report
temperatures on Windows). You can ask ALICE "how hot was the GPU while I was playing DCS yesterday?"; it looks at the
flight-mode periods with the `metrics` tool.

## Modes

- **flight** while DCS or War Thunder is open: only flight-related and critical notifications get through, and ALICE
  doesn't use the local GPU.
- **dev** while Unity or an editor is open.
- **quiet** at night (default `00:30-08:00`): warnings only, no sound.

Muted notifications are not lost: `alice core status` or "what happened while I was in the game?" shows them. The
program lists and policies are in the `[core]` section of `config.toml`.

## Rules

Built-in rules: the GPU or CPU stays very hot for a few minutes, RAM stays full for a long time, a disk is about to fill
up, a program keeps growing its memory. Set up your own with a sentence:

```powershell
alice rules add "tell me if the GPU goes over 80 degrees for 5 minutes and DCS isn't open"
alice rules                  # list;  alice rules off gpu-hot  /  alice rules on gpu-hot  /  alice rules rm <id>
```

Or just tell ALICE "warn me if my heart rate stays high for a long time". Decisions go from cheap to expensive: first
the rule, then the fast decision model (Jev) when the situation is unclear, and ALICE itself only when it really needs
research.

## Scheduled jobs

```powershell
alice tasks add "every Sunday night zip the projects under D:\Unity into E:\Backup"
alice tasks                  # list and last results;  alice tasks run <id>  /  alice tasks rm <id>
```

ALICE turns the sentence into a schedule and shows it; it is set up if you confirm. A job missed while the computer
was off is made up once at startup. Jobs run unattended, so steps that need approval (dangerous commands in auto
mode) are refused; the result arrives as a notification. `alice tasks run <id>` runs the job on the core's next round.
