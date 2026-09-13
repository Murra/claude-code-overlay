"""Configuration for claude-code-overlay.

Every tunable lives here as a dataclass field so that the UI, the parser and
the build script all read from one place.  Settings are persisted as JSON in
``%USERPROFILE%/.claude-code-overlay/config.json`` and are merged on top of the
defaults at load time, so a partial (or slightly out-of-date) config file never
prevents the app from starting.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger(__name__)

APP_NAME = "Claude Code Overlay"
APP_ID = "claude-code-overlay"
APP_VERSION = "1.0.0"

#: Root of the Claude Code state directory.  Overridable for testing via the
#: ``CLAUDE_CONFIG_DIR`` environment variable, which Claude Code itself honours.
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))

#: Where the overlay keeps its own config + cached CLI state.
APP_HOME = Path.home() / f".{APP_ID}"
CONFIG_PATH = APP_HOME / "config.json"
CACHE_PATH = APP_HOME / "cache.json"
LOG_PATH = APP_HOME / "overlay.log"


@dataclass
class WindowConfig:
    """Geometry and chrome of the frameless overlay."""

    width: int = 262
    height: int = 40
    corner_radius: int = 8
    border_width: int = 1

    #: Which painter to use: "rows", "tracks" or "compact".
    layout: str = "rows"

    #: Left inset from the screen edge, in device-independent pixels.
    margin_x: int = 8
    #: Bottom inset. Measured from the screen edge when the overlay sits on the
    #: taskbar, otherwise from the top of the taskbar.
    margin_y: int = 4

    #: Sit *over* the taskbar rather than above it. The Windows 11 taskbar is
    #: 48 logical px tall, so a 40 px overlay clears it with 4 px to spare.
    anchor_over_taskbar: bool = True

    #: Extra allowance when the taskbar height cannot be detected.
    taskbar_fallback: int = 48

    #: Persisted custom position (``None`` means "use the anchored default").
    pos_x: int | None = None
    pos_y: int | None = None

    always_on_top: bool = True
    start_hidden: bool = False
    show_tray_icon: bool = True
    opacity: float = 0.94
    #: Click-through mode is off by default; the window must accept right-clicks.
    frameless: bool = True


@dataclass
class PollingConfig:
    """How often each data source is refreshed, in milliseconds."""

    #: Local ``.jsonl`` session scan. This feeds the tooltip breakdown only —
    #: the overlay face shows plan limits — so it does not need to be fast.
    session_interval_ms: int = 5_000
    #: CLI status probe — expensive, so it runs rarely.
    cli_interval_ms: int = 120_000
    #: Hard timeout for the CLI subprocess, in seconds.
    cli_timeout_s: float = 5.0
    #: Skip files that have not been touched in this many seconds (0 = never skip).
    session_max_age_s: int = 0
    #: Upper bound on lines read from a single session file per poll.
    max_lines_per_file: int = 250_000


@dataclass
class ThemeConfig:
    """Colours are plain ``#rrggbb`` / ``#aarrggbb`` strings understood by Qt."""

    background: str = "#e6151719"
    border: str = "#33ffffff"
    text_primary: str = "#f5f6f7"
    text_secondary: str = "#9aa1a8"
    accent: str = "#d97757"

    #: Usage-percentage thresholds (inclusive lower bounds) mapped to colours.
    ok_color: str = "#4ade80"
    warn_color: str = "#fbbf24"
    critical_color: str = "#f87171"
    warn_threshold: float = 60.0
    critical_threshold: float = 85.0

    font_family: str = "Segoe UI"
    font_size_primary: int = 12
    font_size_secondary: int = 9

    #: Type scale for the compact taskbar layout.
    font_size_value: int = 9
    font_size_label: int = 7

    #: Unfilled portion of a progress bar.
    track: str = "#2b3036"
    #: Row background in the "tracks" layout.
    row_background: str = "#20242899"

    def color_for(self, percent: float | None) -> str:
        """Return the threshold colour for ``percent`` (``None`` -> secondary)."""
        if percent is None:
            return self.text_secondary
        if percent >= self.critical_threshold:
            return self.critical_color
        if percent >= self.warn_threshold:
            return self.warn_color
        return self.ok_color


@dataclass
class ParserConfig:
    """Knobs for the token-parsing engine."""

    #: Directory holding per-project session transcripts.
    projects_dir: str = str(CLAUDE_HOME / "projects")

    #: Additional transcript directories to scan, merged with the one above.
    extra_projects_dirs: List[str] = field(default_factory=list)

    #: On Windows, also scan ``\\wsl.localhost\<distro>\home\<user>\.claude``.
    #: Claude Code running inside WSL writes its logs there, not to the Windows
    #: user profile, so without this a Windows overlay finds nothing.
    auto_discover_wsl: bool = True

    #: How long a successful directory discovery is reused, in seconds.
    #: Discovery is retried far sooner while nothing has been found.
    discovery_ttl_s: int = 300

    #: Count cache reads/writes towards the session total.  Cache reads are
    #: billed at a fraction of the input price, so this is off by default and
    #: the raw numbers are still exposed in the tooltip.
    include_cache_tokens: bool = False

    #: Only aggregate the newest session file.  When False, every session
    #: modified within ``session_window_s`` is summed.
    latest_session_only: bool = True
    session_window_s: int = 3_600

    #: How many recent transcripts to look at before giving up on finding one
    #: that contains actual token usage.  Sessions that record no assistant
    #: turns are skipped: the CLI probe below creates one such transcript every
    #: time it runs, and those would otherwise mask the real session forever.
    max_session_candidates: int = 8

    #: Approximate context budget used for the local percentage readout.
    context_window_tokens: int = 200_000

    #: Candidate CLI invocations, tried in order until one yields a percentage.
    #: ``/usage`` is first because it is the one that actually reports quota:
    #: ``claude -p /status`` replies "/status isn't available in this
    #: environment" and burns ~2.5s doing it. ``/status`` is kept as a fallback
    #: in case a future release reverses that.
    cli_commands: List[List[str]] = field(
        default_factory=lambda: [
            ["claude", "-p", "/usage"],
            ["claude", "-p", "/status"],
        ]
    )
    cli_enabled: bool = True


@dataclass
class Config:
    window: WindowConfig = field(default_factory=WindowConfig)
    polling: PollingConfig = field(default_factory=PollingConfig)
    theme: ThemeConfig = field(default_factory=ThemeConfig)
    parser: ParserConfig = field(default_factory=ParserConfig)

    # ---------------------------------------------------------------- loading

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        """Load config from disk, falling back to defaults on any problem."""
        path = path or CONFIG_PATH
        cfg = cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cfg
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable config at %s: %s", path, exc)
            return cfg
        if not isinstance(raw, dict):
            log.warning("Ignoring non-object config at %s", path)
            return cfg
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Config":
        """Merge ``raw`` over the defaults, ignoring unknown/ill-typed keys."""
        cfg = cls()
        for section in fields(cls):
            values = raw.get(section.name)
            if not isinstance(values, dict):
                continue
            target = getattr(cfg, section.name)
            known = {f.name: f for f in fields(target)}
            for key, value in values.items():
                if key not in known:
                    log.debug("Unknown config key %s.%s", section.name, key)
                    continue
                try:
                    setattr(target, key, value)
                except Exception:  # pragma: no cover - dataclasses are permissive
                    log.debug("Rejected config value %s.%s", section.name, key)
        return cfg

    # ----------------------------------------------------------------- saving

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def save(self, path: Path | None = None) -> bool:
        """Persist config atomically.  Returns True on success."""
        path = path or CONFIG_PATH
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
            )
            os.replace(tmp, path)
            return True
        except OSError as exc:
            log.warning("Could not save config to %s: %s", path, exc)
            return False


def configure_logging(verbose: bool = False) -> None:
    """Attach a rotating-ish file handler plus stderr output.

    Logging must never be the reason the overlay fails to start, so every step
    is guarded and a bare stderr logger is the fallback.
    """
    level = logging.DEBUG if verbose else logging.INFO
    handlers: List[logging.Handler] = [logging.StreamHandler()]
    try:
        APP_HOME.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 1_000_000:
            LOG_PATH.unlink()
        handlers.append(logging.FileHandler(LOG_PATH, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
