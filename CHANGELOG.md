# Changelog

All notable changes to this kit are documented here.
This project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.1] — 2026-05-31

### Added
- **Local-mode punctuation.** Chinese transcripts now get punctuation:
  - a prompt-guided default (`WHISPER_PROMPT`) makes whisper.cpp emit punctuation;
  - half-width punctuation adjacent to CJK is normalized to full-width 「，。！？」
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
