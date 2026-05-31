# whisper-input MCP server

A deliberately **thin** [MCP](https://modelcontextprotocol.io) server that lets you
monitor and configure the dictation service from any MCP client (e.g. **Claude Desktop**) —
no UI, no database, no extra daemon. It just wraps the app's `.env` file and `launchctl`,
and the client spawns it on demand over stdio.

![status tool answering from Claude Desktop](../docs/mcp-status.png)

## Tools

| Tool | What it does |
|---|---|
| `status` | Is the service running? pid, current model, sounds, punctuation, hotkey |
| `logs` | Last N lines of the service error log |
| `get_config` | Read the relevant `.env` settings |
| `set_config` | Set one allow-listed `.env` key (then `restart`) |
| `restart` | Restart the launchd service to apply changes |
| `recent_transcriptions` | Most recent results the service produced |
| `list_models` | Downloaded whisper models + which is current |
| `download_model` | Pull a `ggml-*` model from HuggingFace |
| `set_model` | Switch model (downloads first if missing), then `restart` |

`set_config` only writes an allow-listed set of keys (sounds, model path, punctuation,
archive retention, a few feature toggles) — it cannot write arbitrary keys.

## Requirements

The MCP SDK in the app's venv:

```bash
~/Whisper-Input-Next/.venv/bin/python -m pip install "mcp[cli]"
```

## Register with Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` and add an entry
under `mcpServers` (adjust the two paths and the label to match your install):

```json
{
  "mcpServers": {
    "whisper-input": {
      "command": "/Users/you/Whisper-Input-Next/.venv/bin/python",
      "args": ["/path/to/whisper-input-next-mac-kit/mcp/server.py"],
      "env": {
        "WIN_APP_DIR": "/Users/you/Whisper-Input-Next",
        "WIN_LABEL": "com.whisper-input-next.kit"
      }
    }
  }
}
```

> ⚠️ **Claude Desktop rewrites this file while running** and can drop hand edits. Edit it
> only while Claude Desktop is **fully quit** (⌘Q), then reopen.

`WIN_LABEL` must match the launchd label you installed with (the kit default is
`com.whisper-input-next.kit`).

Then fully quit & reopen Claude Desktop and ask it something like *"What's my dictation
service status?"* — it should call the `status` tool.

## Environment

| Var | Default | Meaning |
|---|---|---|
| `WIN_APP_DIR` | `~/Whisper-Input-Next` | the upstream app checkout |
| `WIN_LABEL` | `com.whisper-input-next.kit` | launchd label of the service |
