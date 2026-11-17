"""Connections: layer for converting a site/service into an ALICE tool via a TOML definition.

* `manifest.py` — reads and validates definition files (built-ins: `builtin/*.toml`, user's:
  `~/.alice/connect/*.toml`).
* `runner.py`   — executes the action: builds the request, attaches credentials, formats the response for the model.
* `oauth.py`    — OAuth2 (Authorization Code + PKCE), via browser and local callback redirect URL.
* `store.py`    — tokens stored in `~/.alice/connect.json` (chmod 600).
* `tool.py`     — each connection is a single tool; actions are selected via the `action` parameter.

No code needed to add a new service: `alice connect <name> --new` creates a template.
"""

from __future__ import annotations

from alice.connect.manifest import Manifest, ManifestError, discover
from alice.connect.runner import ConnectError, ready
from alice.connect.tool import ConnectTool, connector_tools

__all__ = ["ConnectError", "ConnectTool", "Manifest", "ManifestError", "connector_tools", "discover", "ready"]
