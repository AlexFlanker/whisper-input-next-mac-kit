# Changelog

All notable changes to this kit are documented here.
This project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.4] — 2026-05-31

### Added
- **Second indicator style: `capsule`** — a pill that appears while recording (with three
  pulsing dots), **retracts** into a small circle + spinner while transcribing, then turns
  **green and fades** on success. Pick your style with **`INDICATOR_STYLE`** (`ring` |
  `capsule`, default `ring`); the app selects it via a small factory. New kit-owned UI file
  `src/ui/capsule_indicator.py` (MIT), copied in by `install.sh`.
- **Switch the indicator from Claude Desktop** — the MCP server gains a `set_indicator_style`
  tool (`ring` / `capsule` / `off`) that writes the config **and** restarts in one call;
  `INDICATOR_STYLE` is in the writable allowlist and the current style shows in `status`.

## [0.1.3] — 2026-05-31

### Added
- **On-screen listening indicator** — a bottom-center, click-through overlay that floats over
  any app: a breathing ring while recording, a spinner while transcribing, and a green
  expand-and-fade burst when the text is pasted. Fully transparent (no panel chrome) with a
  soft glow so it blends into the background. Toggle with `SHOW_INDICATOR` (default on); also
  settable from the MCP server. The UI component (`src/ui/listening_indicator.py`) is
  kit-owned (MIT) and copied in by `install.sh`; the patcher only wires the `main.py` hooks.

## [0.1.2] — 2026-05-31

### Added
- **MCP server** (`mcp/server.py`) to monitor & configure the dictation service from Claude
  Desktop (or any MCP client) by just asking — `status`, `logs`, `get_config`/`set_config`,
  `restart`, `recent_transcriptions`, and model management (`list`/`download`/`set`). No UI.
- **`install-mcp.sh`** — installs the `mcp` SDK into the app venv and *merges* a
  `whisper-input` entry into Claude Desktop's config (existing servers untouched, backed up,
  idempotent). Refuses to run while Claude Desktop is open (it rewrites its own config).
- **Audio-archive auto-cleanup** (patched into `src/audio/archive.py`): deletes recordings
  older than `AUDIO_ARCHIVE_RETENTION_HOURS` (default 24) at startup and every
  `AUDIO_ARCHIVE_CLEANUP_INTERVAL_HOURS` (default 6), and prunes `cache.json` to match.

### Changed
- **Sound cues are now env-configurable** via `SOUND_START` / `SOUND_STOP` / `SOUND_DONE` /
  `SOUND_ERROR` / `SOUND_WARNING` (defaults unchanged: Submarine/Submarine/Glass/Basso/Funk).
  The patcher's sound block became env-driven to match.
- `templates/env.local.template` now ships the `SOUND_*` and `AUDIO_ARCHIVE_*` keys.

## [0.1.1] — 2026-05-31

### Added
- **Local-mode punctuation.** Chinese transcripts now get punctuation:
  - a prompt-guided default (`WHISPER_PROMPT`) makes whisper.cpp emit punctuation;
  - half-width punctuation adjacent to CJK is normalized to full-width 「，。！？：；」
    (`WHISPER_FULLWIDTH_PUNCT`, on by default).
  Both are configurable in `.env`.

## [0.1.0] — 2026-05-31

Initial release.

### Added
- **One-command installer** (`install.sh`): clones upstream Whisper-Input-Next at a pinned
  commit, sets up a uv (Python 3.12) venv + dependencies, installs Homebrew `whisper-cpp`,
  downloads a whisper model, and renders a per-machine `.env` and launchd LaunchAgent.
- **Idempotent patcher** (`scripts/apply_enhancements.py`) that layers this kit's
  enhancements onto an upstream checkout without redistributing upstream source:
  - single-tap **Right-Command** start/stop toggle;
  - macOS **sound cues** (Submarine on start/stop, Glass on done);
  - **Ctrl+F → local** whisper.cpp routing;
  - fix for upstream `start.sh` dependency check.
- **launchd auto-start service**: login-start, crash-restart, no terminal window.
- `uninstall.sh`, templates, bilingual README (EN + 中文), `CREDITS.md` with an honest
  note on the upstream license status.

### Notes
- macOS only (uses launchd, afplay, Homebrew, pyobjc).
- Default model: `large-v3-turbo` (override with `WIN_MODEL`).
- Targets upstream commit `5edec44bd66ca0a75c75c485d5af2fd201ce8c17`.
