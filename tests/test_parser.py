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
    collect_session_usage,
    find_latest_session_file,
    format_percent,
    format_tokens,
    iter_session_files,
    parse_limit_output,
    parse_session_file,
)


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
    cfg = ParserConfig(projects_dir=str(tmp_path / "absent"))
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

    cfg = ParserConfig(projects_dir=str(projects), latest_session_only=False)
    usage = collect_session_usage(cfg, PollingConfig())
    assert usage.input_tokens == 127
    assert usage.output_tokens == 83


def test_context_percent_is_clamped() -> None:
    usage = SessionUsage(input_tokens=500_000, output_tokens=0, ok=True)
    assert usage.context_percent(200_000, include_cache=False) == 100.0
    assert SessionUsage(ok=False).context_percent(200_000, include_cache=False) is None


@pytest.mark.parametrize(
    "text, weekly, session",
    [
        ("Current session: 12% used\nWeekly limit: 47% used", 47.0, 12.0),
        ("\x1b[32mWeekly (all models): 8.5%\x1b[0m", 8.5, None),
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
    limits = parse_limit_output("Weekly limit: 30%\nOpus weekly limit: 12%")
    assert limits.weekly_percent == 30.0
    assert limits.opus_percent == 12.0
    assert limits.headline_percent == 30.0


def test_limit_cache_roundtrip() -> None:
    original = LimitUsage(weekly_percent=42.0, session_percent=5.0, ok=True, checked_at=1.0)
    restored = LimitUsage.from_dict(original.to_dict())
    assert restored.weekly_percent == 42.0
    assert restored.from_cache is True


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
    assert cfg.window.height == 65


def test_theme_thresholds() -> None:
    theme = Config().theme
    assert theme.color_for(None) == theme.text_secondary
    assert theme.color_for(10.0) == theme.ok_color
    assert theme.color_for(70.0) == theme.warn_color
    assert theme.color_for(95.0) == theme.critical_color
