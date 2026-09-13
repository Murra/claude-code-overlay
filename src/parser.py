"""Token-parsing engine for claude-code-overlay.

Two independent data sources feed the overlay:

1. **Local session transcripts** — Claude Code appends one JSON object per line
   to ``~/.claude/projects/<slug>/<session-id>.jsonl``.  Assistant records carry
   a ``message.usage`` block with ``input_tokens``, ``output_tokens`` and the
   two cache counters.  Reading these is fast, offline and always available.

2. **Plan / weekly limits via the CLI** — a best-effort probe that shells out to
   ``claude`` in a worker thread with a hard timeout.  ``/status`` and
   ``/usage`` are interactive slash commands, so this may legitimately produce
   nothing; when it does, the last successful result is served from an on-disk
   cache and the UI simply shows ``--``.

Everything in this module is defensive by construction: a missing directory, a
half-written line, a file locked by another process or a hung subprocess all
degrade to a partial result plus a status string, never an exception that
reaches the event loop.
"""

from __future__ import annotations

import json
import locale
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from config import CACHE_PATH, Config, ParserConfig, PollingConfig

log = logging.getLogger(__name__)

#: Strips ANSI SGR/CSI sequences from CLI output before regexing it.
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

#: ``Current week (all models): 24% used`` and friends.
PERCENT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")

#: Trailing ``resets Sep 19, 12pm (America/New_York)`` on a usage line.
RESET_RE = re.compile(r"resets?\s+(?:at\s+|in\s+|on\s+)?(.+?)\s*$", re.IGNORECASE)

_WEEKLY_HINTS = ("week", "7-day", "seven day")
_SESSION_HINTS = ("session", "5-hour", "five hour", "current block")
_OPUS_HINTS = ("opus",)


def _human(n: int) -> str:
    """Compact token count: 812 -> '812', 12_400 -> '12.4k', 2_100_000 -> '2.1M'."""
    if n < 1_000:
        return str(n)
    if n < 1_000_000:
        value = n / 1_000
        return f"{value:.1f}k" if value < 100 else f"{value:.0f}k"
    return f"{n / 1_000_000:.1f}M"


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass
class SessionUsage:
    """Aggregated token counts for one or more local session transcripts."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    messages: int = 0
    models: List[str] = field(default_factory=list)

    session_id: str = ""
    project: str = ""
    source_path: str = ""
    mtime: float = 0.0

    ok: bool = False
    status: str = "Scanning..."
    skipped_lines: int = 0

    @property
    def billable_tokens(self) -> int:
        """Input + output, excluding cache traffic."""
        return self.input_tokens + self.output_tokens

    @property
    def total_tokens(self) -> int:
        """Everything the API reported, cache included."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_tokens
            + self.cache_read_tokens
        )

    def display_total(self, include_cache: bool) -> int:
        return self.total_tokens if include_cache else self.billable_tokens

    def context_percent(self, window: int, include_cache: bool) -> Optional[float]:
        """Rough share of a context window consumed by this session."""
        if not self.ok or window <= 0:
            return None
        return min(100.0, 100.0 * self.display_total(include_cache) / window)


@dataclass
class LimitUsage:
    """Plan limits as reported by the Claude Code CLI, when obtainable."""

    session_percent: Optional[float] = None
    weekly_percent: Optional[float] = None
    opus_percent: Optional[float] = None

    #: Reset stamps exactly as printed, e.g. "Sep 19, 12pm (America/New_York)".
    session_resets: str = ""
    weekly_resets: str = ""

    ok: bool = False
    status: str = "--"
    from_cache: bool = False
    checked_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_percent": self.session_percent,
            "weekly_percent": self.weekly_percent,
            "opus_percent": self.opus_percent,
            "session_resets": self.session_resets,
            "weekly_resets": self.weekly_resets,
            "checked_at": self.checked_at,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "LimitUsage":
        def _pct(key: str) -> Optional[float]:
            value = raw.get(key)
            return float(value) if isinstance(value, (int, float)) else None

        return cls(
            session_percent=_pct("session_percent"),
            weekly_percent=_pct("weekly_percent"),
            opus_percent=_pct("opus_percent"),
            session_resets=str(raw.get("session_resets") or ""),
            weekly_resets=str(raw.get("weekly_resets") or ""),
            checked_at=float(raw.get("checked_at") or 0.0),
            ok=True,
            status="cached",
            from_cache=True,
        )

    @property
    def headline_percent(self) -> Optional[float]:
        """Whichever limit is closest to biting."""
        values = [v for v in (self.weekly_percent, self.session_percent) if v is not None]
        return max(values) if values else None

    @property
    def resets_at(self) -> str:
        """Reset stamp belonging to the headline figure."""
        if self.weekly_percent is not None and self.weekly_resets:
            if self.session_percent is None or self.weekly_percent >= self.session_percent:
                return self.weekly_resets
        return self.session_resets or self.weekly_resets

    def session_countdown(self, now: Optional[datetime] = None) -> str:
        return format_countdown(self.session_resets, now)

    def weekly_countdown(self, now: Optional[datetime] = None) -> str:
        return format_countdown(self.weekly_resets, now)



#: ``Sep 19, 12pm (America/New_York)`` / ``Sep 13, 10:20pm`` / ``10:20pm``.
_STAMP_RE = re.compile(
    r"^(?:(?P<month>[A-Za-z]{3,9})\s+(?P<day>\d{1,2})\s*,?\s*)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm)?"
    r"(?:\s*\((?P<tz>[^)]+)\))?\s*$",
    re.IGNORECASE,
)

_MONTHS = {
    name.lower(): index
    for index, name in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        start=1,
    )
}


def _zone(name: Optional[str]):
    """Resolve an IANA zone name, or ``None`` to mean "treat as local time".

    Windows ships no tz database, so ``ZoneInfo("America/New_York")`` raises
    unless the ``tzdata`` package happens to be installed. That is not worth a
    hard dependency: the CLI prints the stamp in the user's own local zone, so
    falling back to naive local time gives the same answer on the machine that
    matters.
    """
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name.strip())
    except Exception:
        return None


def parse_reset_stamp(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """Turn a printed reset stamp into a datetime in the local zone.

    The stamp carries no year, so the nearest sensible future date is chosen:
    a month/day that has already passed rolls into next year, and a bare
    time-of-day that has passed rolls to tomorrow.
    """
    if not text:
        return None
    match = _STAMP_RE.match(text.strip())
    if not match:
        return None

    now = now or datetime.now()
    parts = match.groupdict()

    try:
        hour = int(parts["hour"])
        minute = int(parts["minute"] or 0)
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    meridiem = (parts["meridiem"] or "").lower()
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if hour > 23:
        return None

    zone = _zone(parts["tz"])
    reference = now.astimezone(zone) if zone else now

    month_name = parts["month"]
    if month_name:
        month = _MONTHS.get(month_name[:3].lower())
        if not month:
            return None
        try:
            day = int(parts["day"])
            target = reference.replace(
                month=month, day=day, hour=hour, minute=minute,
                second=0, microsecond=0,
            )
        except ValueError:
            return None
        if (target - reference) < timedelta(days=-180):
            try:
                target = target.replace(year=target.year + 1)
            except ValueError:
                return None
    else:
        target = reference.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= reference:
            target += timedelta(days=1)

    if zone is not None:
        target = target.astimezone()
        return target.replace(tzinfo=None)
    return target


def format_countdown(text: str, now: Optional[datetime] = None) -> str:
    """Time remaining until a reset stamp, as ``5d 6h`` / ``2h 14m`` / ``45m``.

    Falls back to a shortened form of the original text when the stamp cannot
    be parsed, so an upstream format change degrades to something still useful
    rather than to nothing.
    """
    target = parse_reset_stamp(text, now)
    if target is None:
        return text.split("(")[0].strip()[:12]

    remaining = target - (now or datetime.now())
    seconds = int(remaining.total_seconds())
    if seconds <= 0:
        return "now"

    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3_600)
    minutes = seconds // 60
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m" if minutes else "<1m"


# --------------------------------------------------------------------------- #
# Local transcript parsing
# --------------------------------------------------------------------------- #


def _decode_console(raw: bytes) -> str:
    """Decode Windows console output, which ``wsl.exe`` emits as UTF-16-LE."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or (len(raw) > 1 and raw[1:2] == b"\x00"):
        try:
            return raw.decode("utf-16-le", errors="replace")
        except ValueError:
            pass
    return raw.decode("utf-8", errors="replace")


def list_wsl_distros(timeout_s: float = 5.0) -> List[str]:
    r"""Names of *running* WSL distributions.

    Deliberately ``--running`` rather than every installed distro: touching
    ``\\wsl.localhost\<distro>`` boots a stopped distribution, and spinning up a
    VM to look for log files nobody is writing would be a rude thing for a
    desktop widget to do.  A distro with an active Claude Code session is
    running by definition.
    """
    if os.name != "nt":
        return []
    try:
        completed = subprocess.run(
            ["wsl.exe", "--list", "--quiet", "--running"],
            capture_output=True,
            timeout=timeout_s,
            creationflags=_subprocess_flags(),
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("Could not list WSL distros: %s", exc)
        return []
    if completed.returncode != 0:
        return []

    text = _decode_console(completed.stdout or b"").replace("\x00", "")
    return [line.strip() for line in text.splitlines() if line.strip()]


def discover_wsl_projects_dirs(timeout_s: float = 5.0) -> List[Path]:
    """Find ``.claude/projects`` inside every running WSL distribution."""
    found: List[Path] = []
    for distro in list_wsl_distros(timeout_s):
        root = Path(f"\\\\wsl.localhost\\{distro}")
        candidates: List[Path] = []
        try:
            home = root / "home"
            if home.is_dir():
                candidates.extend(entry / ".claude" / "projects" for entry in home.iterdir())
        except OSError as exc:
            log.debug("Could not enumerate %s: %s", root, exc)
        candidates.append(root / "root" / ".claude" / "projects")

        for candidate in candidates:
            try:
                if candidate.is_dir():
                    found.append(candidate)
            except OSError:
                continue
    if found:
        log.info("Discovered WSL transcript directories: %s", ", ".join(map(str, found)))
    return found


#: ``(key, paths, timestamp)`` — discovery is comparatively expensive, so it is
#: cached between polls rather than repeated every two seconds.  The key is
#: derived from the settings that feed discovery, so editing the config (or a
#: test pointing at a different directory) invalidates it immediately.
_dir_cache: Tuple[Optional[tuple], List[Path], float] = (None, [], 0.0)


def _discovery_key(cfg: ParserConfig) -> tuple:
    return (cfg.projects_dir, tuple(cfg.extra_projects_dirs), cfg.auto_discover_wsl)


def reset_discovery_cache() -> None:
    """Force the next :func:`resolve_projects_dirs` call to re-scan."""
    global _dir_cache
    _dir_cache = (None, [], 0.0)


def resolve_projects_dirs(cfg: ParserConfig, timeout_s: float = 5.0) -> List[Path]:
    """Every transcript directory worth scanning, newest discovery cached.

    Order: the configured directory, any explicit extras, then WSL. The retry
    interval collapses to 15s while nothing has been found, so starting WSL
    after the overlay does not leave it blank for the full TTL.
    """
    global _dir_cache

    key = _discovery_key(cfg)
    cached_key, cached, stamp = _dir_cache
    if cached_key == key and stamp:
        ttl = max(15, cfg.discovery_ttl_s) if cached else 15
        if (time.time() - stamp) < ttl:
            return cached

    dirs: List[Path] = []

    def add(path: Path) -> None:
        try:
            if path.is_dir() and path not in dirs:
                dirs.append(path)
        except OSError:
            pass

    add(Path(cfg.projects_dir).expanduser())
    for extra in cfg.extra_projects_dirs:
        add(Path(extra).expanduser())
    if cfg.auto_discover_wsl:
        for path in discover_wsl_projects_dirs(timeout_s):
            add(path)

    _dir_cache = (key, dirs, time.time())
    return dirs


def iter_session_files(projects_dir: "Path | Iterable[Path]") -> List[Path]:
    """Return every ``.jsonl`` transcript under the given root(s), newest first.

    Accepts a single directory or several, so a Windows overlay can merge its
    own profile with one or more WSL homes.  Uses ``os.scandir`` so a directory
    that disappears mid-walk (Claude Code rotates these) is skipped rather than
    raising.
    """
    roots = [projects_dir] if isinstance(projects_dir, Path) else list(projects_dir)
    results: List[Tuple[float, Path]] = []

    stack: List[Path] = []
    for root in roots:
        try:
            if root.is_dir():
                stack.append(root)
        except OSError:
            continue
    if not stack:
        return []

    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.name.endswith(".jsonl"):
                            results.append((entry.stat().st_mtime, Path(entry.path)))
                    except OSError:
                        continue
        except OSError as exc:
            log.debug("Skipping unreadable directory %s: %s", current, exc)
            continue

    results.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in results]


def find_latest_session_file(projects_dir: Path) -> Optional[Path]:
    """Newest transcript, or ``None`` when nothing is readable."""
    files = iter_session_files(projects_dir)
    return files[0] if files else None


def _read_lines(path: Path, max_lines: int) -> Tuple[List[str], Optional[str]]:
    """Read a transcript defensively.

    Claude Code may hold the file open for append while we read it.  On Windows
    that is fine for readers, but a half-flushed final line is common, so the
    caller tolerates trailing garbage.  Returns ``(lines, error)``.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as handle:
            lines: List[str] = []
            for index, line in enumerate(handle):
                if max_lines and index >= max_lines:
                    log.debug("Truncated %s at %d lines", path.name, max_lines)
                    break
                lines.append(line)
            return lines, None
    except PermissionError:
        return [], "File locked"
    except FileNotFoundError:
        return [], "Session ended"
    except OSError as exc:
        log.debug("Read error on %s: %s", path, exc)
        return [], "Read error"


def _extract_usage(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Pull the usage block out of a transcript record, if it has one.

    Handles both the current shape (``{"type": "assistant", "message": {...}}``)
    and a flattened ``{"usage": {...}}`` shape seen in older transcripts.
    """
    message = record.get("message")
    if isinstance(message, dict):
        usage = message.get("usage")
        if isinstance(usage, dict):
            return {"usage": usage, "model": message.get("model")}
    usage = record.get("usage")
    if isinstance(usage, dict):
        return {"usage": usage, "model": record.get("model")}
    return None


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def parse_session_file(
    path: Path,
    max_lines: int = 250_000,
    seen_request_ids: Optional[set] = None,
) -> SessionUsage:
    """Aggregate token usage from a single ``.jsonl`` transcript.

    Corrupted lines are counted and skipped.  Records are de-duplicated by
    ``requestId`` (then ``message.id``) because Claude Code rewrites a record
    when a streamed response is finalised, and a naive sum double-counts those.
    """
    usage = SessionUsage(
        session_id=path.stem,
        project=path.parent.name,
        source_path=str(path),
    )
    try:
        usage.mtime = path.stat().st_mtime
    except OSError:
        pass

    lines, error = _read_lines(path, max_lines)
    if error:
        usage.status = error
        return usage
    if not lines:
        usage.status = "Empty session"
        usage.ok = True
        return usage

    seen = seen_request_ids if seen_request_ids is not None else set()
    models: List[str] = []

    for line in lines:
        line = line.strip()
        if not line or line[0] != "{":
            continue
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            usage.skipped_lines += 1
            continue
        if not isinstance(record, dict):
            usage.skipped_lines += 1
            continue

        found = _extract_usage(record)
        if not found:
            continue

        message = record.get("message")
        key = record.get("requestId")
        if not key and isinstance(message, dict):
            key = message.get("id")
        if not key:
            key = record.get("uuid")
        if key:
            token = (str(path), str(key))
            if token in seen:
                continue
            seen.add(token)

        block = found["usage"]
        usage.input_tokens += _as_int(block.get("input_tokens"))
        usage.output_tokens += _as_int(block.get("output_tokens"))
        usage.cache_creation_tokens += _as_int(block.get("cache_creation_input_tokens"))
        usage.cache_read_tokens += _as_int(block.get("cache_read_input_tokens"))
        usage.messages += 1

        model = found.get("model")
        if isinstance(model, str) and model and model not in models:
            models.append(model)

    usage.models = models
    usage.ok = True
    usage.status = "ok"
    if usage.skipped_lines:
        log.debug("%s: skipped %d malformed lines", path.name, usage.skipped_lines)
    return usage


def _newest_session_with_usage(
    files: List[Path], cfg: ParserConfig, polling: PollingConfig
) -> SessionUsage:
    """Newest transcript that actually recorded token usage.

    "Newest file wins" is not quite right, because not every transcript
    contains a conversation.  ``claude -p /usage`` -- the CLI probe this very
    module runs to read plan limits -- writes a fresh transcript every time it
    is invoked, and those contain no assistant turns at all.  They are always
    the newest file on disk, so a naive newest-file rule locks the overlay onto
    an empty session and reports zero tokens indefinitely, while the user's real
    session sits one slot further down the list.

    So: walk newest-first and take the first transcript with at least one usage
    record, keeping the newest readable one as a fallback.
    """
    fallback: Optional[SessionUsage] = None
    limit = max(1, cfg.max_session_candidates)

    for path in files[:limit]:
        usage = parse_session_file(path, polling.max_lines_per_file)
        if usage.ok and usage.messages > 0:
            return usage
        if fallback is None and usage.ok:
            fallback = usage

    if fallback is not None:
        fallback.status = "No usage yet"
        return fallback
    return parse_session_file(files[0], polling.max_lines_per_file)


def collect_session_usage(cfg: ParserConfig, polling: PollingConfig) -> SessionUsage:
    """Top-level local scan honouring the ``latest_session_only`` setting."""
    projects_dirs = resolve_projects_dirs(cfg, polling.cli_timeout_s)
    if not projects_dirs:
        return SessionUsage(status="No ~/.claude logs", ok=False)

    files = iter_session_files(projects_dirs)
    if not files:
        return SessionUsage(status="No sessions yet", ok=False)

    if cfg.latest_session_only:
        return _newest_session_with_usage(files, cfg, polling)

    cutoff = time.time() - max(1, cfg.session_window_s)
    seen: set = set()
    combined = SessionUsage(ok=True, status="ok")
    combined.session_id = files[0].stem
    combined.project = files[0].parent.name
    combined.source_path = str(files[0])

    for path in files:
        try:
            if path.stat().st_mtime < cutoff:
                break  # files are newest-first, so everything after is older
        except OSError:
            continue
        part = parse_session_file(path, polling.max_lines_per_file, seen)
        if not part.ok:
            continue
        combined.input_tokens += part.input_tokens
        combined.output_tokens += part.output_tokens
        combined.cache_creation_tokens += part.cache_creation_tokens
        combined.cache_read_tokens += part.cache_read_tokens
        combined.messages += part.messages
        combined.skipped_lines += part.skipped_lines
        combined.mtime = max(combined.mtime, part.mtime)
        for model in part.models:
            if model not in combined.models:
                combined.models.append(model)

    if combined.messages == 0:
        combined.status = "No usage yet"
    return combined


# --------------------------------------------------------------------------- #
# CLI limit probing
# --------------------------------------------------------------------------- #


def parse_limit_output(text: str) -> LimitUsage:
    """Extract session / weekly / Opus percentages from CLI output.

    The output format of ``/status`` is not a stable API, so this matches on
    keywords per line and tolerates anything it does not recognise.
    """
    result = LimitUsage()
    if not text:
        result.status = "no output"
        return result

    clean = ANSI_RE.sub("", text)
    weekly_reset = ""
    session_reset = ""
    for raw_line in clean.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = PERCENT_RE.search(line)
        if not match:
            continue
        try:
            percent = float(match.group(1))
        except ValueError:
            continue
        if not 0.0 <= percent <= 100.0:
            continue

        # Read the reset stamp off the same line, in its original case, so it
        # stays legible ("Sep 19, 12pm (America/New_York)").
        reset_match = RESET_RE.search(line)
        reset = reset_match.group(1).strip(" .")[:48] if reset_match else ""

        lowered = line.lower()
        if any(hint in lowered for hint in _OPUS_HINTS) and result.opus_percent is None:
            result.opus_percent = percent
        elif any(hint in lowered for hint in _WEEKLY_HINTS) and result.weekly_percent is None:
            result.weekly_percent = percent
            weekly_reset = weekly_reset or reset
        elif any(hint in lowered for hint in _SESSION_HINTS) and result.session_percent is None:
            result.session_percent = percent
            session_reset = session_reset or reset

    result.session_resets = session_reset
    result.weekly_resets = weekly_reset

    if (
        result.weekly_percent is None
        and result.session_percent is None
        and result.opus_percent is None
    ):
        result.status = "unrecognised output"
        return result

    result.ok = True
    result.status = "ok"
    result.checked_at = time.time()
    return result


def _decode_output(raw: Optional[bytes]) -> str:
    """Decode CLI output without mangling it.

    The Claude CLI separates fields with U+00B7, and on Windows the bytes are
    not reliably UTF-8 — decoding with a fixed codec turns that into a
    replacement character and corrupts the reset timestamp alongside it. Try
    UTF-8 first, then the console's own encoding, then give up gracefully.
    """
    if not raw:
        return ""
    for encoding in ("utf-8", locale.getpreferredencoding(False), "cp1252"):
        if not encoding:
            continue
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _subprocess_flags() -> int:
    """Suppress the console window that would otherwise flash on Windows."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def run_cli_probe(cfg: ParserConfig, timeout_s: float) -> LimitUsage:
    """Try each configured CLI command until one yields usable percentages."""
    if not cfg.cli_enabled:
        return LimitUsage(status="disabled")

    last_status = "CLI unavailable"
    for command in cfg.cli_commands:
        if not command:
            continue
        try:
            completed = subprocess.run(
                list(command),
                capture_output=True,
                timeout=timeout_s,
                creationflags=_subprocess_flags(),
                stdin=subprocess.DEVNULL,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            log.debug("CLI probe timed out: %s", command)
            last_status = "CLI timeout"
            continue
        except FileNotFoundError:
            log.debug("CLI not on PATH: %s", command[0])
            last_status = "claude not found"
            continue
        except OSError as exc:
            log.debug("CLI probe failed (%s): %s", command, exc)
            last_status = "CLI error"
            continue

        merged = _decode_output(completed.stdout) + "\n" + _decode_output(completed.stderr)
        parsed = parse_limit_output(merged)
        if parsed.ok:
            return parsed
        last_status = parsed.status

    return LimitUsage(status=last_status)


def load_cached_limits(path: Path = CACHE_PATH) -> LimitUsage:
    """Read the last successful CLI result from disk."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return LimitUsage(status="--")
    if not isinstance(raw, dict):
        return LimitUsage(status="--")
    try:
        return LimitUsage.from_dict(raw)
    except (TypeError, ValueError):
        return LimitUsage(status="--")


def save_cached_limits(limits: LimitUsage, path: Path = CACHE_PATH) -> None:
    """Persist a successful CLI result so a later failure can fall back to it."""
    if not limits.ok:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(limits.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.debug("Could not cache limits: %s", exc)


# --------------------------------------------------------------------------- #
# Qt workers
# --------------------------------------------------------------------------- #


class SessionScanWorker(QObject):
    """Runs :func:`collect_session_usage` off the GUI thread."""

    finished = pyqtSignal(object)  # SessionUsage

    def __init__(self, config: Config) -> None:
        super().__init__()
        self._config = config

    def run(self) -> None:
        try:
            usage = collect_session_usage(self._config.parser, self._config.polling)
        except Exception:  # pragma: no cover - last-resort guard
            log.exception("Session scan crashed")
            usage = SessionUsage(status="Scan error")
        self.finished.emit(usage)


class CliProbeWorker(QObject):
    """Runs the CLI probe off the GUI thread, with disk-cache fallback."""

    finished = pyqtSignal(object)  # LimitUsage

    def __init__(self, config: Config) -> None:
        super().__init__()
        self._config = config

    def run(self) -> None:
        try:
            limits = run_cli_probe(self._config.parser, self._config.polling.cli_timeout_s)
        except Exception:  # pragma: no cover - last-resort guard
            log.exception("CLI probe crashed")
            limits = LimitUsage(status="CLI error")

        if limits.ok:
            save_cached_limits(limits)
        else:
            cached = load_cached_limits()
            if cached.ok:
                cached.status = limits.status
                cached.from_cache = True
                limits = cached
        self.finished.emit(limits)


class UsageMonitor(QObject):
    """Owns both workers and guarantees only one of each runs at a time.

    Each poll spins up a ``QThread`` that is torn down when the worker emits,
    so a hung probe can never block the next one or leak into shutdown.
    """

    session_ready = pyqtSignal(object)  # SessionUsage
    limits_ready = pyqtSignal(object)  # LimitUsage

    def __init__(self, config: Config, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._config = config
        self._session_thread: Optional[QThread] = None
        self._cli_thread: Optional[QThread] = None
        # The workers are moveToThread'd and therefore cannot have a parent, so
        # nothing but these attributes keeps them alive. Without them Python
        # collects the worker the moment the starting method returns, the
        # started -> run connection dies silently, and the thread sits idle
        # forever with no error anywhere.
        self._session_worker: Optional[QObject] = None
        self._cli_worker: Optional[QObject] = None
        self._closing = False

    # -- public API ------------------------------------------------------

    def refresh_session(self) -> None:
        if self._closing or self._session_thread is not None:
            return
        worker = SessionScanWorker(self._config)
        self._session_worker = worker
        self._session_thread = self._start(
            worker, self._on_session_done, self._on_session_thread_finished
        )

    def refresh_limits(self) -> None:
        if self._closing or self._cli_thread is not None:
            return
        if not self._config.parser.cli_enabled:
            self.limits_ready.emit(load_cached_limits())
            return
        worker = CliProbeWorker(self._config)
        self._cli_worker = worker
        self._cli_thread = self._start(
            worker, self._on_limits_done, self._on_cli_thread_finished
        )

    def refresh_all(self) -> None:
        self.refresh_session()
        self.refresh_limits()

    def shutdown(self, wait_ms: int = 2_000) -> None:
        """Stop accepting work and join any in-flight threads.

        Must genuinely join them: Qt aborts the process if a QThread is
        destroyed while still running, which is what happens on exit if a scan
        is in flight.  Safe to call more than once.
        """
        self._closing = True
        for thread in (self._session_thread, self._cli_thread):
            if thread is None:
                continue
            try:
                thread.quit()
                if not thread.wait(wait_ms):
                    log.warning("Worker thread did not stop within %dms", wait_ms)
            except RuntimeError:
                # The underlying C++ object was already deleted by Qt.
                pass
        self._session_thread = None
        self._cli_thread = None
        self._session_worker = None
        self._cli_worker = None

    # -- internals -------------------------------------------------------

    def _start(self, worker: QObject, callback, on_thread_finished) -> QThread:
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(callback)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        # Our references are released on QThread.finished, not on the worker's
        # own signal. The worker finishes first; the thread is still running
        # its event loop at that point, and dropping the reference early leaves
        # shutdown() with nothing to join.
        thread.finished.connect(on_thread_finished)
        thread.finished.connect(thread.deleteLater)
        thread.start()
        return thread

    def _on_session_thread_finished(self) -> None:
        self._session_thread = None
        self._session_worker = None

    def _on_cli_thread_finished(self) -> None:
        self._cli_thread = None
        self._cli_worker = None

    def _on_session_done(self, usage: SessionUsage) -> None:
        if not self._closing:
            self.session_ready.emit(usage)

    def _on_limits_done(self, limits: LimitUsage) -> None:
        if not self._closing:
            self.limits_ready.emit(limits)


# --------------------------------------------------------------------------- #
# Formatting helpers used by the UI
# --------------------------------------------------------------------------- #


def format_tokens(usage: SessionUsage, include_cache: bool) -> str:
    """Primary line text, e.g. ``'12.4k tok'`` or ``'--'`` on failure."""
    if not usage.ok:
        return "--"
    return _human(usage.display_total(include_cache))


def format_io(usage: SessionUsage) -> str:
    """Secondary line detail, e.g. ``'in 9.1k / out 3.3k'``."""
    if not usage.ok:
        return "waiting for logs"
    return f"in {_human(usage.input_tokens)} / out {_human(usage.output_tokens)}"


def format_percent(percent: Optional[float]) -> str:
    if percent is None:
        return "--"
    if percent >= 99.5:
        return "100%"
    return f"{percent:.0f}%" if percent >= 10 else f"{percent:.1f}%"


def build_tooltip(usage: SessionUsage, limits: LimitUsage, cfg: Config) -> str:
    """Rich HTML tooltip with every number we have."""
    rows: List[Tuple[str, str]] = []
    if limits.session_percent is not None:
        rows.append(
            ("Session", f"{format_percent(limits.session_percent)}"
                        f"  ·  resets {limits.session_resets or '?'}")
        )
    if limits.weekly_percent is not None:
        rows.append(
            ("Week", f"{format_percent(limits.weekly_percent)}"
                     f"  ·  resets {limits.weekly_resets or '?'}")
        )
    if limits.opus_percent is not None:
        rows.append(("Opus week", format_percent(limits.opus_percent)))
    if not limits.ok:
        rows.append(("Plan usage", limits.status))
    elif limits.from_cache:
        rows.append(("Plan usage", "cached"))

    rows += [
        ("Input", f"{usage.input_tokens:,}"),
        ("Output", f"{usage.output_tokens:,}"),
        ("Cache write", f"{usage.cache_creation_tokens:,}"),
        ("Cache read", f"{usage.cache_read_tokens:,}"),
        ("Messages", f"{usage.messages:,}"),
    ]
    if usage.models:
        rows.append(("Model", ", ".join(usage.models[:2])))
    if usage.project:
        rows.append(("Project", usage.project))
    if usage.session_id:
        rows.append(("Session", usage.session_id[:8]))
    if not usage.ok:
        rows.append(("Local logs", usage.status))
    if usage.skipped_lines:
        rows.append(("Skipped lines", str(usage.skipped_lines)))

    body = "".join(
        f"<tr><td style='padding-right:10px;color:#9aa1a8'>{label}</td>"
        f"<td align='right'>{value}</td></tr>"
        for label, value in rows
    )
    return f"<table style='font-family:{cfg.theme.font_family}'>{body}</table>"
