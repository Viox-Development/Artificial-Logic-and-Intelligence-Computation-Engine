# Home: computer, Wi-Fi, TV and phone

Written from the "Ev: bilgisayar, Wi-Fi, TV ve telefon" section of the Turkish guide
([docs/README.tr.md](../README.tr.md)) and checked against `alice/tools/home.py` and `alice/home/`. The Turkish
section is written for Windows; this page adds the Linux backends that were added since (#239).

ALICE also manages things outside the computer, all by chat or voice: "turn off the screen", "switch to performance
mode", "set brightness to 40 percent", "who is on the network", "open Netflix on the TV", "how much charge does my
phone have".

## Computer (`pc`)

Lock, screen off, sleep, hibernate, restart, power plan (balanced, high performance, power saver) and monitor
brightness. On an external monitor brightness is set over DDC/CI, which must be enabled in the monitor's own menu; on
a laptop screen WMI is used. Sleep, hibernate and restart ask for approval.

On Linux the same actions use loginctl, systemctl, the display server's DPMS command (xset on X11; KDE, GNOME, Sway
and Hyprland on Wayland), powerprofilesctl and brightnessctl, whichever is installed.

## Wi-Fi and the home network (`wifi`)

Connection status and signal, nearby networks, saved networks, connecting to a network (with a password if it isn't
saved) and disconnecting. "Who is on the network" lists the devices on the subnet with IP, MAC and name where known.
Windows uses `netsh wlan` and reads it the same on Turkish or English Windows; Linux uses NetworkManager (`nmcli`).

## Turning the computer on remotely (Wake-on-LAN)

While the computer is off, ALICE is off too, so another device has to send the wake packet; in practice a WoL app on
your phone. `pc wol_info` shows the MAC, the broadcast address and whether the network card is ready to wake the
computer. It works reliably on a wired computer. "Wake on LAN / Power On By PCI-E" must be enabled in the BIOS, "Wake
on Magic Packet" in the network card's Power Management tab, and Windows Fast Startup turned off. ALICE itself can wake
another device on the network with `wifi wake`.

## Android TV and phone (`android`, ADB)

- Install adb on the computer: `winget install Google.PlatformTools`.
- **TV:** Settings > Device Preferences > About > tap Build number 7 times, then turn on network (or USB) debugging in
  Developer options. Then say "Alice, find the TV" (`find` scans the network for port 5555) or give its IP. On the
  first connection choose "Always allow" in the window on the TV. The address is saved as `[android] tv`.
- **Phone (Android 11+):** Developer options > Wireless debugging > "Pair device with pairing code". `pair` once with
  the IP:port and code on the screen, then `connect` with the IP:port on the Wireless debugging main screen. The port
  changes every time; ALICE finds the same IP's new port over mDNS by itself.
- Both can: power on/off, keys (volume, play/pause, arrows, home), open apps (by name, e.g. "exxen"), open a YouTube
  video, type text, take a screenshot. On the phone also: battery status, reading notifications and sending a file from
  the computer to the Downloads folder.

## Samsung TV (`tv`)

Uses Samsung's local remote control API, so nothing needs installing on the TV (`pip install websockets` or
`alice-harness[home]`). With the TV on, say "Alice, find the TV" and then connect; choose "Allow" in the permission
question on the TV. The token, IP and MAC address are saved under `[samsung_tv]`. It can: power on/off, volume,
channels, arrow keys, source/HDMI, open any app installed on the TV by name, open a YouTube video and type into an open
keyboard. When the TV is off its network card sleeps too, so turning it on uses Wake-on-LAN; for that, enable Settings
> General > Network > Expert Settings > "Power On with Mobile" on the TV.

## Modem (`wifi modem`)

You don't need to know the model. The modem announces itself over UPnP (on by default on most home modems), and ALICE
reads the brand and model, the external IP, the connection state, how long the modem has been up, the line speed and
the traffic. If UPnP is off, it guesses from the title of the admin page.

**ZTE modems (ZXHN H3600, H1600):** on the ZTE modems Superonline provides, ALICE can also log in to the admin
interface. It lists the devices the modem knows, wired or Wi-Fi with their names ("who is connected to the modem"),
and restarts the modem (asks for approval). Add a `MODEM_PASSWORD=...` line to `~/.alice/.env` (the password is usually
on the label under the modem). If the username isn't `admin`, change it with `[modem] user`. The password is never
written to the config and never sent to the AI model.
