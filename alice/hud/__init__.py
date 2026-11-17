"""On-screen HUD: a separate small process drawing arrows, cards, frames over games, desktop, and browser.

  alice hud            Start HUD (core starts it automatically if [hud] enabled = true)
  alice hud --demo     Draw arrow, edge glow, card, and progress bar on desktop (30 s)

The rest of the code only says "show" (alice.hud.client.show); HUD knows how to draw it. HUD does not inject code into games,
does not read their memory: it only draws its own transparent window (like Discord or OBS overlay). The window
passes clicks through, takes no focus, does not appear on taskbar or ALICE's screenshots.

Commands come via UDP to 127.0.0.1 as JSON: {"kind": "arrow", "clock": 4, "text": "SAM 12 km", "ttl": 3}.
"""

PORT = 41240
