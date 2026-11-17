"""ALICE core: background service running continuously.

  store     -> storage and querying of streaming data (metrics/events) in SQLite
  monitors  -> observers collecting data (computer: CPU, GPU, RAM, disk, processes)
  modes     -> environment-based mode (flight, development, silent, normal) and mode policy
  rules     -> "notify if condition persists this long" rules; Jev if ambiguous, ALICE if needed
  schedule  -> cron-based scheduled tasks, compensating for missed runs
  notify    -> notification channels (desktop notification, log, voice in future)
  service   -> loop running all components at intervals, system tray, auto-start
"""
