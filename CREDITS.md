# Credits & Attribution

`whisper-input-next-mac-kit` is **only an installer + enhancement layer**. All credit for
the actual voice-transcription tool belongs to the upstream authors listed below.

## This kit does NOT redistribute upstream source code

`install.sh` clones the upstream project directly from its **official repository** onto your
machine, then applies a small, idempotent set of enhancements via
`scripts/apply_enhancements.py`. Nothing in this repository is a copy of upstream's source
files — only references to them (a pinned commit hash and short anchor strings used to locate
insertion points).

## Upstream projects (please star/support them)

- **Whisper-Input-Next** — the tool this kit installs and enhances.
  <https://github.com/Mor-Li/Whisper-Input-Next> · author **Mor-Li**
- **Whisper-Input** — the original project Whisper-Input-Next is based on.
  <https://github.com/ErlichLiu/Whisper-Input> · author **ErlichLiu**
- **whisper.cpp** — the local speech-to-text engine (installed via Homebrew `whisper-cpp`).
  <https://github.com/ggml-org/whisper.cpp> · **Georgi Gerganov** & contributors

## License status — please read

At the time of writing, **neither Whisper-Input-Next nor Whisper-Input ships an explicit
LICENSE file** (GitHub reports no detected license for either), even though
Whisper-Input-Next's README shows an "MIT" badge. Source code published without an explicit
license is, by default, **"all rights reserved."**

Because of that ambiguity, this kit deliberately **does not copy or redistribute upstream
code** — it only automates fetching it from the official source and layering our own
additions on top, on your own machine. If you intend to build on or redistribute the
upstream code yourself, please review the upstream repositories' terms and consider asking
the authors to add a clear LICENSE.

The `LICENSE` (MIT) in this repository covers **only the original code in this kit** —
the installer, patcher, templates, and documentation.

> This note is a practical, good-faith description, not legal advice.
