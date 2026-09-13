<div align="center">

# Claude Code Overlay

**A frameless, always-on-top desktop widget that shows your live Claude Code token usage on Windows 11.**

[![Release](https://img.shields.io/github/v/release/USERNAME/claude-code-overlay?style=flat-square)](https://github.com/USERNAME/claude-code-overlay/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg?style=flat-square)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg?style=flat-square)](https://www.python.org/downloads/)
[![Build](https://img.shields.io/github/actions/workflow/status/USERNAME/claude-code-overlay/release.yml?style=flat-square)](../../actions)

<!-- Replace with a real capture: 220x65 overlay in the bottom-left corner while a Claude Code session runs. -->
<img src="docs/demo.gif" alt="Claude Code Overlay running in the bottom-left corner of a Windows 11 desktop" width="640">

*Add `docs/demo.gif` — a ~6 second capture of the counter climbing during a live session.*

</div>

---

## What it does

Claude Code writes a JSON-lines transcript of every session to `~/.claude/projects/`. Each
assistant turn carries the exact token accounting the API returned. This overlay tails
those files and puts the number on your desktop, so you can see what a session is costing
you without leaving your editor or typing `/status`.

```
┌────────────────────────────────────┐
│ ● 37.6k tokens               47%   │
│   in 36 / out 37.6k        weekly  │
│ ████████████████░░░░░░░░░░░░░░░░░  │
└────────────────────────────────────┘
          220 x 65 pixels
```

## Features

| | |
|---|---|
| **Live local counts** | Reads `~/.claude/projects/**/*.jsonl` every 2 seconds. Input, output and both cache counters, de-duplicated by request ID. |
| **Plan limits** | Best-effort `claude` CLI probe on a worker thread with a hard 5-second timeout, cached to disk between runs. |
| **Always visible, never in the way** | `Tool` window flag keeps it off the taskbar and out of Alt-Tab. Stays on top until you toggle it off. |
| **Drag anywhere** | Anchored bottom-left by default; drag it somewhere else and the position sticks. |
| **Threshold colours** | Green under 60%, amber under 85%, red above. All three thresholds are configurable. |
| **Tray icon** | Show/hide, refresh, or start minimized with `--minimized`. |
| **Scroll to fade** | Mouse-wheel over the widget adjusts opacity. |
| **Never crashes on bad data** | Missing logs, truncated lines, locked files and CLI timeouts all degrade to `--` or a short status string. |

## Quick start

### Download the executable

1. Grab **`ClaudeCodeOverlay.exe`** from the [latest release](../../releases/latest).
2. Double-click it. There is no installer and it needs no admin rights.
3. The overlay appears in the bottom-left corner.

Windows SmartScreen flags unsigned binaries the first time you run one. Either choose
**More info → Run anyway**, or verify the published hash first:

```powershell
Get-FileHash .\ClaudeCodeOverlay.exe -Algorithm SHA256
```

### Run from source

```bash
git clone https://github.com/USERNAME/claude-code-overlay.git
cd claude-code-overlay

python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -r requirements.txt
python src/main.py
```

Python 3.10 or newer. PyQt6 ships prebuilt wheels for every supported platform, so there is
nothing to compile.

### Command-line flags

| Flag | Effect |
|---|---|
| `--minimized` | Start hidden; use the tray icon to show it. |
| `--no-tray` | Skip the tray icon entirely. |
| `--reset-position` | Ignore the saved position and re-anchor to bottom-left. |
| `--verbose` | Debug logging to `~/.claude-code-overlay/overlay.log`. |
| `--version` | Print the version and exit. |

## Using it

| Action | Result |
|---|---|
| Left-drag | Move the overlay. The position is saved on release. |
| Double-click | Force an immediate refresh. |
| Scroll wheel | Fade in / out. |
| Right-click | Refresh Now · Always on Top · Reset Position · Count Cache Tokens · Hide · Exit |
| Hover | Tooltip with the full breakdown: input, output, cache read/write, message count, model, project and session ID. |
| Tray click | Show / hide the overlay. |

**Count Cache Tokens** is off by default. Prompt-cache reads are billed at a fraction of
normal input tokens, so including them in the headline number makes a long session look far
more expensive than it is. The raw cache figures are always in the tooltip.

## Configuration

Settings live in `%USERPROFILE%\.claude-code-overlay\config.json`, written on first exit.
Anything you leave out falls back to the default, and an unparseable file is ignored rather
than fatal — so it is safe to edit by hand.

```jsonc
{
  "window": {
    "width": 220,
    "height": 65,
    "corner_radius": 10,
    "margin_x": 12,          // inset from the left edge of the work area
    "margin_y": 12,          // inset from the top of the taskbar
    "pos_x": null,           // set automatically when you drag the widget
    "pos_y": null,
    "always_on_top": true,
    "start_hidden": false,
    "show_tray_icon": true,
    "opacity": 0.94
  },
  "polling": {
    "session_interval_ms": 2000,   // local log scan
    "cli_interval_ms": 120000,     // plan-limit probe
    "cli_timeout_s": 5.0,          // hard kill for the subprocess
    "max_lines_per_file": 250000
  },
  "theme": {
    "ok_color": "#4ade80",
    "warn_color": "#fbbf24",
    "critical_color": "#f87171",
    "warn_threshold": 60.0,
    "critical_threshold": 85.0,
    "accent": "#d97757",
    "font_family": "Segoe UI"
  },
  "parser": {
    "include_cache_tokens": false,
    "latest_session_only": true,   // false sums every session from the last hour
    "session_window_s": 3600,
    "context_window_tokens": 200000,
    "cli_enabled": true
  }
}
```

Set `CLAUDE_CONFIG_DIR` to point the parser at a Claude Code state directory other than
`~/.claude` — the same variable Claude Code itself honours.

## Architecture

```
                 ┌──────────────────────────────┐
                 │  ~/.claude/projects/          │
                 │    <project-slug>/            │
                 │      <session-uuid>.jsonl     │
                 └──────────────┬───────────────┘
                                │  read-only, every 2s
                 ┌──────────────▼───────────────┐        ┌────────────────────┐
                 │  parser.SessionScanWorker     │        │ parser.CliProbe-   │
                 │  • newest file by mtime       │        │ Worker             │
                 │  • json.loads per line        │        │ • claude -p /status│
                 │  • skip malformed lines       │        │ • 5s timeout       │
                 │  • de-dup by requestId        │        │ • disk cache       │
                 └──────────────┬───────────────┘        └─────────┬──────────┘
                                │  QThread                          │  QThread
                 ┌──────────────▼──────────────────────────────────▼──────────┐
                 │                   parser.UsageMonitor                       │
                 │      one worker of each kind at a time; signals only        │
                 └──────────────────────────┬─────────────────────────────────┘
                                            │  Qt signals (GUI thread)
                 ┌──────────────────────────▼─────────────────────────────────┐
                 │                  main.OverlayWindow                         │
                 │        custom paintEvent · drag · tray · context menu       │
                 └────────────────────────────────────────────────────────────┘
```

### How the log parsing works

Every line in a session transcript is one JSON object. The ones that matter look like this:

```json
{
  "type": "assistant",
  "requestId": "req_011Cf25FkCX88FovVKXFnD7c",
  "timestamp": "2026-09-13T20:31:48.619Z",
  "message": {
    "model": "claude-opus-5",
    "usage": {
      "input_tokens": 2,
      "output_tokens": 278,
      "cache_creation_input_tokens": 318,
      "cache_read_input_tokens": 38839
    }
  }
}
```

Three details make the difference between a number you can trust and one you cannot:

1. **De-duplication.** A single assistant turn is often written to the transcript more than
   once — once per content block, and again when a streamed response is finalised. Each copy
   carries the *same* `usage` object. Summing naively inflates the total, sometimes by 2-3x,
   so records are keyed on `requestId` (falling back to `message.id`, then `uuid`) and counted
   once.

2. **Cache tokens are separate.** `cache_read_input_tokens` routinely dwarfs `input_tokens` by
   two orders of magnitude, and is billed at roughly a tenth of the price. The headline number
   is `input + output`; cache traffic is opt-in via the context menu.

3. **The file is live.** Claude Code holds the transcript open and appends to it. Reads are
   non-exclusive on Windows, but the last line is frequently a partial write. Every line is
   parsed inside a `try`, malformed ones are counted in `skipped_lines`, and the total is still
   returned.

### About the plan-limit readout

`/status` and `/usage` are *interactive* slash commands inside a Claude Code session — there is
no stable non-interactive command that prints your weekly quota. The CLI probe tries
`claude -p /status`, then `claude -p /usage`, scrapes any percentage it recognises, and caches
the result in `~/.claude-code-overlay/cache.json`.

If neither works on your setup, the badge shows `--` and falls back to a local context-window
estimate. **Local token counting is unaffected** — it never touches the CLI. Set
`parser.cli_enabled` to `false` to skip the probe entirely.

## Building the executable

```bash
pip install -r requirements.txt
python build.py --clean
```

Output lands at `dist/ClaudeCodeOverlay.exe` — one file, roughly 35 MB, with the icon
embedded and no console window.

| Flag | Effect |
|---|---|
| `--clean` | Wipe `build/` and `dist/` first. |
| `--onedir` | Folder build instead of one-file — starts faster, easier to debug. |
| `--console` | Keep a console window so tracebacks are visible. |
| `--no-upx` | Disable UPX compression (some antivirus engines dislike packed binaries). |

Anything after the flags is passed straight through to PyInstaller.

### Releasing

`.github/workflows/release.yml` runs tests on Windows and Linux, builds the binary on
`windows-latest`, and publishes a GitHub Release with the `.exe` plus its SHA-256 whenever a
`v*.*.*` tag is pushed:

```bash
git tag v1.0.0
git push origin v1.0.0
```

Use **Actions → Release → Run workflow** to build without publishing.

## Development

```
claude-code-overlay/
├── .github/workflows/release.yml   Test + build + publish on tag
├── src/
│   ├── config.py                   Dataclass settings, JSON persistence
│   ├── parser.py                   Log parsing, CLI probe, Qt workers
│   └── main.py                     Frameless window, painting, tray, menu
├── assets/
│   ├── icon.ico                    Multi-resolution, 16 → 256 px
│   └── make_icon.py                Regenerates the icon, zero dependencies
├── tests/test_parser.py            25 tests over the parsing engine
├── build.py                        PyInstaller wrapper
└── requirements.txt
```

```bash
python -m pytest tests -q
```

`tests/test_parser.py` covers the cases that actually happen in the wild: corrupted lines,
truncated writes, duplicate request IDs, negative counts, a missing `~/.claude` directory,
and unparseable CLI output.

The icon is generated, not committed as an opaque blob — `assets/make_icon.py` rasterises it
with nothing but the standard library, so you can change the colours and re-run it.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Shows `No ~/.claude logs` | Claude Code has not run on this machine, or your state directory is elsewhere. Set `CLAUDE_CONFIG_DIR`. |
| Shows `No sessions yet` | The directory exists but holds no transcripts. Start a Claude Code session. |
| Percentage stays `--` | The CLI probe found nothing parseable. Expected on many setups — local counts still work. |
| Counter looks too high | Turn off **Count Cache Tokens** in the right-click menu. |
| Overlay is off-screen after a monitor change | Right-click → **Reset Position**, or run with `--reset-position`. |
| Nothing appears at all | Check `~/.claude-code-overlay/overlay.log`, or run `python src/main.py --verbose`. |
| Hidden behind a fullscreen game | Exclusive-fullscreen apps bypass every always-on-top window. Use borderless windowed mode. |

## Privacy

The overlay reads local files and counts integers. It makes no network requests, sends no
telemetry, and never reads the `content` field of your messages — only the `usage` block. The
only things it writes are its own config, cache and log under `~/.claude-code-overlay/`.

## Contributing

Issues and pull requests are welcome. Please run `python -m pytest tests -q` before opening a
PR, and keep the parser's rule intact: **it must never raise into the event loop.** A new data
source should degrade to a status string, not an exception.

## License

[MIT](LICENSE) © 2026 Michael Murra

Not affiliated with Anthropic. "Claude" and "Claude Code" are trademarks of Anthropic, PBC.
