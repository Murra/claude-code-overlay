"""Tests for the parsing engine — the half of the app that must never crash."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from config import Config, ParserConfig, PollingConfig  # noqa: E402
from parser import (  # noqa: E402
    LimitUsage,
    SessionUsage,
    _decode_console,
    collect_session_usage,
    find_latest_session_file,
    format_percent,
    format_tokens,
    iter_session_files,
    list_wsl_distros,
    parse_limit_output,
    parse_session_file,
    UsageMonitor,
    _probe_commands,
    is_probe_transcript,
    reset_discovery_cache,
    resolve_projects_dirs,
    run_cli_probe,
    sweep_probe_transcripts,
)


@pytest.fixture(autouse=True)
def _clear_discovery_cache():
    """Directory discovery is cached process-wide; isolate every test from it."""
    reset_discovery_cache()
    yield
    reset_discovery_cache()


def _assistant(request_id: str, inp: int, out: int, cc: int = 0, cr: int = 0) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "requestId": request_id,
            "timestamp": "2026-09-13T20:31:48.619Z",
            "message": {
                "role": "assistant",
                "model": "claude-opus-5",
                "usage": {
                    "input_tokens": inp,
                    "output_tokens": out,
                    "cache_creation_input_tokens": cc,
                    "cache_read_input_tokens": cr,
                },
            },
        }
    )


@pytest.fixture()
def session_file(tmp_path: Path) -> Path:
    project = tmp_path / "projects" / "-c-Users-dev-app"
    project.mkdir(parents=True)
    path = project / "abc123.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}),
                _assistant("req_1", 100, 50, cc=10, cr=200),
                _assistant("req_2", 20, 30),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_aggregates_input_and_output(session_file: Path) -> None:
    usage = parse_session_file(session_file)
    assert usage.ok
    assert usage.input_tokens == 120
    assert usage.output_tokens == 80
    assert usage.cache_creation_tokens == 10
    assert usage.cache_read_tokens == 200
    assert usage.messages == 2
    assert usage.billable_tokens == 200
    assert usage.total_tokens == 410
    assert usage.models == ["claude-opus-5"]


def test_deduplicates_repeated_request_ids(session_file: Path) -> None:
    with session_file.open("a", encoding="utf-8") as handle:
        handle.write(_assistant("req_2", 20, 30) + "\n")
    usage = parse_session_file(session_file)
    assert usage.messages == 2, "a rewritten streamed record must not double-count"


def test_survives_corrupt_and_partial_lines(session_file: Path) -> None:
    with session_file.open("a", encoding="utf-8") as handle:
        handle.write("{not json at all\n")
        handle.write("\n")
        handle.write('{"type":"assistant","message":{"usage":')  # truncated write
    usage = parse_session_file(session_file)
    assert usage.ok
    assert usage.input_tokens == 120
    assert usage.skipped_lines >= 2


def test_ignores_negative_and_non_integer_counts(tmp_path: Path) -> None:
    path = tmp_path / "weird.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "requestId": "r",
                "message": {"usage": {"input_tokens": -5, "output_tokens": "many"}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    usage = parse_session_file(path)
    assert usage.ok
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0


def test_missing_file_is_not_fatal(tmp_path: Path) -> None:
    usage = parse_session_file(tmp_path / "nope.jsonl")
    assert not usage.ok
    assert usage.status == "Session ended"
    assert format_tokens(usage, include_cache=False) == "--"


def test_missing_projects_dir_is_not_fatal(tmp_path: Path) -> None:
    cfg = ParserConfig(projects_dir=str(tmp_path / "absent"), auto_discover_wsl=False)
    usage = collect_session_usage(cfg, PollingConfig())
    assert not usage.ok
    assert usage.status == "No ~/.claude logs"


def test_finds_newest_session_across_projects(tmp_path: Path, session_file: Path) -> None:
    projects = session_file.parents[1]
    other = projects / "-c-Users-dev-other"
    other.mkdir()
    newer = other / "def456.jsonl"
    newer.write_text(_assistant("req_9", 1, 1) + "\n", encoding="utf-8")
    import os
    import time

    os.utime(newer, (time.time() + 60, time.time() + 60))

    assert find_latest_session_file(projects) == newer
    assert len(iter_session_files(projects)) == 2


def test_combined_mode_sums_across_recent_sessions(tmp_path: Path, session_file: Path) -> None:
    projects = session_file.parents[1]
    (projects / "-c-b").mkdir()
    (projects / "-c-b" / "s2.jsonl").write_text(_assistant("req_7", 7, 3) + "\n", encoding="utf-8")

    cfg = ParserConfig(
        projects_dir=str(projects), latest_session_only=False, auto_discover_wsl=False
    )
    usage = collect_session_usage(cfg, PollingConfig())
    assert usage.input_tokens == 127
    assert usage.output_tokens == 83


def test_skips_probe_transcripts_that_contain_no_usage(tmp_path: Path) -> None:
    """Regression test for the overlay reporting zero tokens forever.

    ``claude -p /usage`` — the CLI probe this app runs to read plan limits —
    writes a brand-new transcript every time it is invoked, containing no
    assistant turns. It is therefore always the newest file on disk. Taking
    "newest file" literally locks the overlay onto an empty session while the
    user's real conversation sits one slot down.
    """
    import os
    import time

    projects = tmp_path / "projects"
    real = projects / "real-project"
    probe = projects / "probe-project"
    real.mkdir(parents=True)
    probe.mkdir(parents=True)

    (real / "real.jsonl").write_text(_assistant("r1", 500, 250), encoding="utf-8")
    # What a probe transcript looks like: metadata rows, no assistant usage.
    (probe / "probe.jsonl").write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "/usage"}}) + "\n"
        + json.dumps({"type": "summary", "summary": "usage check"}) + "\n",
        encoding="utf-8",
    )
    newer = time.time() + 120
    os.utime(probe / "probe.jsonl", (newer, newer))

    cfg = ParserConfig(projects_dir=str(projects), auto_discover_wsl=False)
    usage = collect_session_usage(cfg, PollingConfig())

    assert usage.input_tokens == 500, "the empty probe transcript masked the real session"
    assert usage.output_tokens == 250
    assert usage.project == "real-project"


def test_reports_no_usage_yet_when_every_candidate_is_empty(tmp_path: Path) -> None:
    projects = tmp_path / "projects" / "p"
    projects.mkdir(parents=True)
    (projects / "a.jsonl").write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n",
        encoding="utf-8",
    )
    cfg = ParserConfig(projects_dir=str(tmp_path / "projects"), auto_discover_wsl=False)
    usage = collect_session_usage(cfg, PollingConfig())
    assert usage.ok
    assert usage.status == "No usage yet"
    assert usage.input_tokens == 0


def test_candidate_search_is_bounded(tmp_path: Path) -> None:
    """A long tail of empty transcripts must not be parsed end to end."""
    import os
    import time

    projects = tmp_path / "projects" / "p"
    projects.mkdir(parents=True)
    (projects / "old-real.jsonl").write_text(_assistant("r", 9, 9), encoding="utf-8")
    for i in range(12):
        empty = projects / f"empty{i:02d}.jsonl"
        empty.write_text(json.dumps({"type": "user"}) + "\n", encoding="utf-8")
        stamp = time.time() + 60 + i
        os.utime(empty, (stamp, stamp))

    cfg = ParserConfig(
        projects_dir=str(tmp_path / "projects"),
        auto_discover_wsl=False,
        max_session_candidates=3,
    )
    usage = collect_session_usage(cfg, PollingConfig())
    assert usage.status == "No usage yet", "search must stop after max_session_candidates"


def test_context_percent_is_clamped() -> None:
    usage = SessionUsage(input_tokens=500_000, output_tokens=0, ok=True)
    assert usage.context_percent(200_000, include_cache=False) == 100.0
    assert SessionUsage(ok=False).context_percent(200_000, include_cache=False) is None


#: Verbatim output of ``claude -p /usage`` (2026-09), the command the probe
#: actually relies on. Tests are written against this rather than an invented
#: format so a change upstream shows up here first.
REAL_USAGE_OUTPUT = (
    "You are currently using your subscription to power your Claude Code usage\n"
    "\n"
    "Current session: 17% used \u00b7 resets Sep 13, 10:20pm (America/New_York)\n"
    "Current week (all models): 24% used \u00b7 resets Sep 19, 12pm (America/New_York)\n"
)


def test_parses_real_usage_output() -> None:
    limits = parse_limit_output(REAL_USAGE_OUTPUT)
    assert limits.ok
    assert limits.session_percent == 17.0
    assert limits.weekly_percent == 24.0
    assert limits.opus_percent is None
    assert limits.headline_percent == 24.0


def test_reset_stamp_comes_from_the_line_that_is_displayed() -> None:
    """The badge shows the weekly figure, so it must show the weekly reset."""
    limits = parse_limit_output(REAL_USAGE_OUTPUT)
    assert limits.resets_at == "Sep 19, 12pm (America/New_York)"


def test_reset_stamp_falls_back_to_session_line() -> None:
    limits = parse_limit_output("Current session: 17% used \u00b7 resets Sep 13, 10:20pm")
    assert limits.session_percent == 17.0
    assert limits.resets_at == "Sep 13, 10:20pm"


def test_status_unavailable_message_is_not_mistaken_for_data() -> None:
    """``claude -p /status`` answers this; it must not read as a usage figure."""
    limits = parse_limit_output("/status isn't available in this environment.\n")
    assert not limits.ok
    assert limits.headline_percent is None


@pytest.mark.parametrize(
    "text, weekly, session",
    [
        ("Current session: 12% used\nWeekly limit: 47% used", 47.0, 12.0),
        ("\x1b[32mCurrent week (all models): 8.5% used\x1b[0m", 8.5, None),
        ("nothing useful here", None, None),
        ("", None, None),
        ("Weekly limit: 400%", None, None),
    ],
)
def test_parse_limit_output(text: str, weekly, session) -> None:
    limits = parse_limit_output(text)
    assert limits.weekly_percent == weekly
    assert limits.session_percent == session
    assert limits.ok == (weekly is not None or session is not None)


def test_parse_limit_output_prefers_opus_line() -> None:
    limits = parse_limit_output(
        "Current week (all models): 30% used\nCurrent week (Opus): 12% used"
    )
    assert limits.weekly_percent == 30.0
    assert limits.opus_percent == 12.0
    assert limits.headline_percent == 30.0


def test_decode_output_handles_non_utf8_console_bytes() -> None:
    from parser import _decode_output

    line = "Current week (all models): 24% used \u00b7 resets Sep 19"
    assert _decode_output(line.encode("utf-8")) == line
    # cp1252 bytes are not valid UTF-8; they must not become replacement chars.
    assert "\ufffd" not in _decode_output(line.encode("cp1252"))
    assert _decode_output(None) == ""
    assert _decode_output(b"") == ""


def test_limit_cache_roundtrip() -> None:
    original = LimitUsage(
        weekly_percent=42.0,
        session_percent=5.0,
        session_resets="Sep 13, 10:20pm",
        weekly_resets="Sep 19, 12pm",
        ok=True,
        checked_at=1.0,
    )
    restored = LimitUsage.from_dict(original.to_dict())
    assert restored.weekly_percent == 42.0
    assert restored.session_resets == "Sep 13, 10:20pm"
    assert restored.weekly_resets == "Sep 19, 12pm"
    assert restored.from_cache is True


def test_headline_tracks_whichever_limit_is_closest_to_biting() -> None:
    assert LimitUsage(session_percent=80.0, weekly_percent=24.0).headline_percent == 80.0
    assert LimitUsage(session_percent=10.0, weekly_percent=24.0).headline_percent == 24.0
    assert LimitUsage(session_percent=10.0).headline_percent == 10.0
    assert LimitUsage().headline_percent is None


@pytest.mark.parametrize(
    "stamp, expected",
    [
        ("Sep 19, 12pm (America/New_York)", "5d 18h"),
        ("Sep 13, 10:20pm (America/New_York)", "4h 25m"),
        ("Sep 13, 10:20pm", "4h 25m"),
        ("10:20pm", "4h 25m"),
        ("Sep 13, 6:00pm", "5m"),
        ("Sep 13, 5:55pm", "now"),
        ("Jan 2, 9am", "110d 15h"),
    ],
)
def test_format_countdown(stamp: str, expected: str) -> None:
    from datetime import datetime

    from parser import format_countdown

    assert format_countdown(stamp, datetime(2026, 9, 13, 17, 55)) == expected


def test_countdown_degrades_to_the_raw_text() -> None:
    """An upstream format change must still show something useful."""
    from datetime import datetime

    from parser import format_countdown, parse_reset_stamp

    now = datetime(2026, 9, 13, 17, 55)
    assert parse_reset_stamp("next tuesday-ish", now) is None
    assert format_countdown("next tuesday-ish", now) == "next tuesday"
    assert format_countdown("", now) == ""


def test_reset_stamp_rolls_over_the_year() -> None:
    from datetime import datetime

    from parser import parse_reset_stamp

    target = parse_reset_stamp("Jan 2, 9am", datetime(2026, 12, 28, 10, 0))
    assert target is not None and target.year == 2027


def test_limit_cache_rejects_garbage() -> None:
    restored = LimitUsage.from_dict({"weekly_percent": "lots", "resets_at": None})
    assert restored.weekly_percent is None
    assert restored.resets_at == ""


@pytest.mark.parametrize(
    "value, expected",
    [(None, "--"), (0.0, "0.0%"), (5.25, "5.2%"), (47.0, "47%"), (99.9, "100%")],
)
def test_format_percent(value, expected) -> None:
    assert format_percent(value) == expected


def test_format_tokens_is_compact() -> None:
    assert format_tokens(SessionUsage(input_tokens=812, ok=True), False) == "812"
    assert format_tokens(SessionUsage(input_tokens=12_400, ok=True), False) == "12.4k"
    assert format_tokens(SessionUsage(input_tokens=2_100_000, ok=True), False) == "2.1M"


def test_config_merge_ignores_unknown_and_keeps_defaults() -> None:
    cfg = Config.from_dict({"window": {"width": 300, "bogus": 1}, "nope": {"x": 1}})
    assert cfg.window.width == 300
    assert cfg.window.height == Config().window.height


def test_theme_thresholds() -> None:
    theme = Config().theme
    assert theme.color_for(None) == theme.text_secondary
    assert theme.color_for(10.0) == theme.ok_color
    assert theme.color_for(70.0) == theme.warn_color
    assert theme.color_for(95.0) == theme.critical_color


# --------------------------------------------------------------------------- #
# Directory discovery (Windows overlay + Claude Code running inside WSL)
# --------------------------------------------------------------------------- #


def test_decode_console_handles_utf16_from_wsl_exe() -> None:
    assert _decode_console(b"U\x00b\x00u\x00n\x00t\x00u\x00") == "Ubuntu"
    assert _decode_console("Debian\n".encode("utf-8")) == "Debian\n"
    assert _decode_console(b"") == ""


def test_list_wsl_distros_is_empty_off_windows(monkeypatch) -> None:
    monkeypatch.setattr("parser.os.name", "posix")
    assert list_wsl_distros() == []


def test_resolve_merges_extras_and_deduplicates(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    primary.mkdir()
    secondary.mkdir()

    cfg = ParserConfig(
        projects_dir=str(primary),
        extra_projects_dirs=[str(secondary), str(primary), str(tmp_path / "absent")],
        auto_discover_wsl=False,
    )
    assert resolve_projects_dirs(cfg) == [primary, secondary]


def test_resolve_skips_wsl_when_disabled(tmp_path: Path, monkeypatch) -> None:
    called = []
    monkeypatch.setattr("parser.discover_wsl_projects_dirs", lambda t: called.append(t) or [])
    cfg = ParserConfig(projects_dir=str(tmp_path), auto_discover_wsl=False)
    resolve_projects_dirs(cfg)
    assert called == []


def test_resolve_includes_discovered_wsl_dirs(tmp_path: Path, monkeypatch) -> None:
    wsl_like = tmp_path / "wsl-home" / ".claude" / "projects"
    wsl_like.mkdir(parents=True)
    monkeypatch.setattr("parser.discover_wsl_projects_dirs", lambda t: [wsl_like])
    cfg = ParserConfig(projects_dir=str(tmp_path / "absent"), auto_discover_wsl=True)
    assert resolve_projects_dirs(cfg) == [wsl_like]


def test_discovery_cache_invalidates_when_config_changes(tmp_path: Path) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()

    assert resolve_projects_dirs(
        ParserConfig(projects_dir=str(first), auto_discover_wsl=False)
    ) == [first]
    assert resolve_projects_dirs(
        ParserConfig(projects_dir=str(second), auto_discover_wsl=False)
    ) == [second], "a changed projects_dir must not serve the cached result"


def test_iter_session_files_accepts_multiple_roots(tmp_path: Path) -> None:
    roots = []
    for name in ("a", "b"):
        root = tmp_path / name
        (root / "proj").mkdir(parents=True)
        (root / "proj" / f"{name}.jsonl").write_text(_assistant("r", 1, 1), encoding="utf-8")
        roots.append(root)

    assert len(iter_session_files(roots)) == 2
    assert len(iter_session_files(roots[0])) == 1, "a single Path must still work"
    assert iter_session_files([tmp_path / "absent"]) == []


def test_collect_merges_across_roots(tmp_path: Path) -> None:
    windows = tmp_path / "win" / "proj"
    wsl = tmp_path / "wsl" / "proj"
    windows.mkdir(parents=True)
    wsl.mkdir(parents=True)
    (windows / "w.jsonl").write_text(_assistant("r1", 10, 5), encoding="utf-8")
    (wsl / "l.jsonl").write_text(_assistant("r2", 3, 2), encoding="utf-8")

    cfg = ParserConfig(
        projects_dir=str(tmp_path / "win"),
        extra_projects_dirs=[str(tmp_path / "wsl")],
        auto_discover_wsl=False,
        latest_session_only=False,
    )
    usage = collect_session_usage(cfg, PollingConfig())
    assert usage.input_tokens == 13
    assert usage.output_tokens == 7


# --------------------------------------------------------------------------- #
# Worker plumbing
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def qt_app():
    """A QCoreApplication is enough for QThread; no display server needed."""
    from PyQt6.QtCore import QCoreApplication

    yield QCoreApplication.instance() or QCoreApplication([])


def _pump(monitor, signal, timeout_ms=10_000):
    """Spin an event loop until ``signal`` fires or the timeout expires."""
    from PyQt6.QtCore import QEventLoop, QTimer

    received = []
    loop = QEventLoop()
    signal.connect(received.append)
    signal.connect(lambda *_: loop.quit())
    QTimer.singleShot(timeout_ms, loop.quit)
    return received, loop


def test_monitor_actually_runs_its_session_worker(qt_app, session_file: Path) -> None:
    """Regression test for a silent PyQt lifetime bug.

    The worker is moveToThread'd, so it cannot have a parent. If UsageMonitor
    does not also hold a Python reference, the worker is garbage-collected the
    instant refresh_session() returns: the thread starts, the started -> run
    connection is already dead, nothing ever runs and no error is raised
    anywhere. The overlay just shows "Scanning..." forever.
    """
    cfg = Config()
    cfg.parser.projects_dir = str(session_file.parents[1])
    cfg.parser.auto_discover_wsl = False

    monitor = UsageMonitor(cfg)
    received, loop = _pump(monitor, monitor.session_ready)
    monitor.refresh_session()
    assert monitor._session_worker is not None, "worker reference must be retained"
    loop.exec()
    monitor.shutdown()

    assert received, "session_ready never fired — the worker did not run"
    assert received[0].input_tokens == 120
    assert received[0].output_tokens == 80
    assert monitor._session_worker is None, "reference must be released when done"
    assert monitor._session_thread is None


def test_monitor_refuses_to_stack_concurrent_scans(qt_app, session_file: Path) -> None:
    cfg = Config()
    cfg.parser.projects_dir = str(session_file.parents[1])
    cfg.parser.auto_discover_wsl = False

    monitor = UsageMonitor(cfg)
    monitor.refresh_session()
    first = monitor._session_thread
    monitor.refresh_session()
    assert monitor._session_thread is first, "a second scan must not start while one runs"
    monitor.shutdown()


def test_monitor_shutdown_is_idempotent_and_silences_signals(qt_app) -> None:
    cfg = Config()
    cfg.parser.auto_discover_wsl = False
    monitor = UsageMonitor(cfg)
    monitor.shutdown()
    monitor.shutdown()

    received, _ = _pump(monitor, monitor.session_ready, timeout_ms=1)
    monitor.refresh_session()
    assert monitor._session_thread is None, "no work may start after shutdown"
    assert received == []


def test_shutdown_joins_a_scan_that_is_still_running(qt_app, session_file: Path) -> None:
    """The application's exit path.

    Qt aborts the whole process if a QThread is destroyed while still running,
    so shutdown() has to actually join. This test starts a scan and tears the
    monitor down immediately, without pumping an event loop in between.
    """
    cfg = Config()
    cfg.parser.projects_dir = str(session_file.parents[1])
    cfg.parser.auto_discover_wsl = False

    monitor = UsageMonitor(cfg)
    monitor.refresh_session()
    assert monitor._session_thread is not None

    monitor.shutdown()
    assert monitor._session_thread is None
    assert monitor._session_worker is None
    del monitor  # must not abort the interpreter


# --------------------------------------------------------------------------- #
# Janitor
# --------------------------------------------------------------------------- #

#: A transcript exactly as ``claude -p /usage`` leaves it: a couple of queue
#: rows, the slash command, and no assistant turn anywhere.
def _probe_lines(command: str = "/usage") -> str:
    return "\n".join(
        [
            json.dumps({"type": "queue-operation"}),
            json.dumps({"type": "queue-operation"}),
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "<local-command-caveat>Caveat: …"},
            }),
            json.dumps({
                "type": "user",
                "message": {
                    "role": "user",
                    "content": f"<command-name>{command}</command-name>\n<command-message>usage",
                },
            }),
            json.dumps({"type": "system"}),
            json.dumps({"type": "last-prompt"}),
        ]
    ) + "\n"


def _write(path: Path, text: str, age_s: float = 300.0) -> Path:
    import os
    import time

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))
    return path


def test_recognises_a_probe_transcript(tmp_path: Path) -> None:
    path = _write(tmp_path / "p.jsonl", _probe_lines())
    assert is_probe_transcript(path, ["/usage", "/status"])


def test_never_deletes_a_real_conversation(tmp_path: Path) -> None:
    """The safety property. A transcript with any assistant turn is untouchable."""
    real = _write(
        tmp_path / "proj" / "real.jsonl",
        _probe_lines() + _assistant("req_1", 100, 50) + "\n",
    )
    assert not is_probe_transcript(real, ["/usage"])
    assert sweep_probe_transcripts(tmp_path, ["/usage"]) == []
    assert real.exists()


def test_ignores_transcripts_the_user_typed_into(tmp_path: Path) -> None:
    """A user message with no slash command means a real, if short, session."""
    chat = _write(
        tmp_path / "proj" / "chat.jsonl",
        json.dumps({"type": "user", "message": {"role": "user", "content": "hello there"}}) + "\n",
    )
    assert not is_probe_transcript(chat, ["/usage"])
    assert sweep_probe_transcripts(tmp_path, ["/usage"]) == []
    assert chat.exists()


def test_recognises_a_rejected_probe_with_no_user_message(tmp_path: Path) -> None:
    """`claude -p /status` is refused and leaves only queue/system rows behind."""
    rejected = _write(
        tmp_path / "proj" / "rejected.jsonl",
        "\n".join(
            json.dumps({"type": t})
            for t in ("queue-operation", "queue-operation", "system", "system", "last-prompt")
        ) + "\n",
    )
    assert is_probe_transcript(rejected, ["/usage", "/status"])


def test_empty_rule_still_respects_assistant_turns(tmp_path: Path) -> None:
    """No user message, but something was generated — keep it."""
    odd = _write(
        tmp_path / "proj" / "odd.jsonl",
        json.dumps({"type": "queue-operation"}) + "\n" + _assistant("r", 10, 10) + "\n",
    )
    assert not is_probe_transcript(odd, ["/usage"])
    assert odd.exists()


def test_ignores_a_different_slash_command(tmp_path: Path) -> None:
    other = _write(tmp_path / "proj" / "other.jsonl", _probe_lines("/compact"))
    assert not is_probe_transcript(other, ["/usage", "/status"])
    assert other.exists()


def test_ignores_large_files_even_if_they_match(tmp_path: Path) -> None:
    big = _write(
        tmp_path / "proj" / "big.jsonl",
        _probe_lines() + json.dumps({"type": "note", "pad": "x" * 70_000}) + "\n",
    )
    assert not is_probe_transcript(big, ["/usage"])
    assert big.exists()


def test_sweep_removes_probe_transcripts(tmp_path: Path) -> None:
    a = _write(tmp_path / "proj" / "a.jsonl", _probe_lines())
    b = _write(tmp_path / "proj" / "b.jsonl", _probe_lines("/status"))
    real = _write(tmp_path / "proj" / "real.jsonl", _assistant("r", 5, 5) + "\n")

    removed = sweep_probe_transcripts(tmp_path, ["/usage", "/status"])
    assert set(removed) == {a, b}
    assert not a.exists() and not b.exists()
    assert real.exists(), "the real transcript must survive"


def test_sweep_leaves_recent_files_alone(tmp_path: Path) -> None:
    """A session being written right now must never be a candidate."""
    fresh = _write(tmp_path / "proj" / "fresh.jsonl", _probe_lines(), age_s=0.0)
    assert sweep_probe_transcripts(tmp_path, ["/usage"], min_age_s=30.0) == []
    assert fresh.exists()
    # ...unless it is explicitly named, which is how a just-finished probe is cleaned.
    assert sweep_probe_transcripts(tmp_path, ["/usage"], only=[fresh]) == [fresh]
    assert not fresh.exists()


def test_sweep_dry_run_reports_without_deleting(tmp_path: Path) -> None:
    a = _write(tmp_path / "proj" / "a.jsonl", _probe_lines())
    assert sweep_probe_transcripts(tmp_path, ["/usage"], dry_run=True) == [a]
    assert a.exists()


def test_sweep_survives_a_missing_directory(tmp_path: Path) -> None:
    assert sweep_probe_transcripts(tmp_path / "absent", ["/usage"]) == []


def test_probe_commands_extracted_from_config() -> None:
    assert _probe_commands(ParserConfig()) == ["/usage", "/status"]
    assert _probe_commands(ParserConfig(cli_commands=[["claude", "--version"]])) == []


def test_probe_run_cleans_up_after_itself(tmp_path: Path, monkeypatch) -> None:
    """End to end: the file a probe creates is gone once the probe returns."""
    projects = tmp_path / "projects" / "proj"
    projects.mkdir(parents=True)
    created = projects / "new-probe.jsonl"

    def fake_run(*args, **kwargs):
        created.write_text(_probe_lines(), encoding="utf-8")

        class Completed:
            returncode = 0
            stdout = (
                b"Current session: 17% used\n"
                b"Current week (all models): 24% used\n"
            )
            stderr = b""

        return Completed()

    monkeypatch.setattr("parser.subprocess.run", fake_run)
    cfg = ParserConfig(projects_dir=str(tmp_path / "projects"), auto_discover_wsl=False)

    limits = run_cli_probe(cfg, timeout_s=5.0)
    assert limits.ok and limits.weekly_percent == 24.0
    assert not created.exists(), "the probe's own transcript must be removed"


def test_cleanup_can_be_switched_off(tmp_path: Path, monkeypatch) -> None:
    projects = tmp_path / "projects" / "proj"
    projects.mkdir(parents=True)
    created = projects / "kept.jsonl"

    def fake_run(*args, **kwargs):
        created.write_text(_probe_lines(), encoding="utf-8")

        class Completed:
            returncode = 0
            stdout = b"Current week (all models): 24% used\n"
            stderr = b""

        return Completed()

    monkeypatch.setattr("parser.subprocess.run", fake_run)
    cfg = ParserConfig(
        projects_dir=str(tmp_path / "projects"),
        auto_discover_wsl=False,
        cleanup_probe_logs=False,
    )
    run_cli_probe(cfg, timeout_s=5.0)
    assert created.exists()
