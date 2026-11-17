"""Smartwatch data (Huawei Watch GT 2 Pro, via Gadgetbridge on the phone).

There are two ways, both writing to the same keys (watch.hr, watch.steps, watch.stress, watch.spo2):
  1. Gadgetbridge's automatic export database (arrives on PC via Syncthing etc.):
     imports new samples as the core file changes. Data every few minutes / hour.
  2. Live: an automation on the phone (Tasker, Automate, HTTP Shortcuts) POSTs heart rate to the `alice phone`
     server. Required for instant response during flight.
`alice watch status` measures how frequently data arrives: this is the "Gadgetbridge test" of the plan.
"""
