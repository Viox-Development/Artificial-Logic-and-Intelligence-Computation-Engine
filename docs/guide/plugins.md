# Plugins

Written from the "Eklentiler" section of the Turkish guide ([docs/README.tr.md](../README.tr.md)).

Adding a new ability doesn't need changes to the core code: put a Python file in `~/.alice/plugins/`.

```powershell
alice plugins new spotify     # template: ~/.alice/plugins/spotify.py
alice plugins                 # installed plugins and their errors
```

Plugin tools stay in their own group and are opened by keywords in your message (the plugin's `keywords`) or with
`load_tools`; in voice conversations they can be open from the start. Details and an example:
[docs/PLUGINS.md](../PLUGINS.md).
