"""Unit tests for the generic fleet auto-update engine."""
from __future__ import annotations

from legends_temporal.fleet_update import (
    evaluate_tool_gate,
     find_busy_sessions,
    format_buzz_summary,
    is_update_available,
    parse_version_tuple,
    verify_and_merge_settings,
)


def test_version_parsing_and_comparison():
    assert parse_version_tuple("2.1.323") == (2, 1, 323)
    assert parse_version_tuple("1.4.20-R8619.1") == (1, 4, 20, 8619, 1)
    assert is_update_available("2.1.323", "2.1.325") is True
    assert is_update_available("1.5.3", "1.5.9") is True
    assert is_update_available("1.5.9", "1.5.9") is False
    assert is_update_available("1.4.20-R8619.1", "1.4.20-R8619.1") is False
    assert is_update_available("2.1.323", "2.1.325", check_only=True) is False


def test_busy_session_filtering_and_window_hunting():
    sessions = [
        {"id": "s1", "host_id": "h-canary", "harness": "claude-sdk", "status": "idle", "archived": False},
        {"id": "s2", "host_id": "h-canary", "harness": "claude-sdk", "status": "running", "archived": False},
        {"id": "s3", "host_id": "h-canary", "harness": "muse", "status": "idle", "archived": False},
    ]
    assert len(find_busy_sessions(sessions, host_id="h-canary", harness="claude-sdk")) == 1
    assert len(find_busy_sessions(sessions, host_id="h-canary", harness="muse")) == 0

    state: dict = {}
    # Busy check resets streak to 0
    g1 = evaluate_tool_gate(
        host_key="canary",
        tool_key="claude_cli",
        host_id="h-canary",
        harness="claude-sdk",
        sessions=sessions,
        hunt_state=state,
        now_epoch=1000.0,
        required_idle_checks=2,
    )
    assert not g1.allowed
    assert g1.state == "pending_busy"

    # First idle check reaches streak 1/2
    idle_sessions = [{"id": "s1", "host_id": "h-canary", "harness": "claude-sdk", "status": "idle", "archived": False}]
    g2 = evaluate_tool_gate(
        host_key="canary",
        tool_key="claude_cli",
        host_id="h-canary",
        harness="claude-sdk",
        sessions=idle_sessions,
        hunt_state=state,
        now_epoch=2200.0,
        required_idle_checks=2,
    )
    assert not g2.allowed
    assert g2.state == "pending_streak"
    assert g2.consecutive_idle_checks == 1

    # Second consecutive idle check reaches streak 2/2 -> allowed!
    g3 = evaluate_tool_gate(
        host_key="canary",
        tool_key="claude_cli",
        host_id="h-canary",
        harness="claude-sdk",
        sessions=idle_sessions,
        hunt_state=state,
        now_epoch=3400.0,
        required_idle_checks=2,
    )
    assert g3.allowed
    assert g3.state == "ready"
    assert "canary:claude_cli" not in state


def test_24h_timeout_gate():
    state = {"workstation:claude_cli": {"pending_since": 1000.0, "consecutive_idle_checks": 0}}
    busy_sessions = [{"id": "s2", "host_id": "h-ws", "harness": "claude-sdk", "status": "running", "archived": False}]
    g = evaluate_tool_gate(
        host_key="workstation",
        tool_key="claude_cli",
        host_id="h-ws",
        harness="claude-sdk",
        sessions=busy_sessions,
        hunt_state=state,
        now_epoch=1000.0 + 86401.0,
        required_idle_checks=2,
    )
    assert not g.allowed
    assert g.state == "expired_24h"
    assert "pending 24 h: always busy" in g.reason


def test_verify_and_merge_settings():
    cur = {"model": "opus", "env": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "80"}}
    req = {
        "includeCoAuthoredBy": False,
        "attribution": {"commit": "", "pr": ""},
        "env": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "80", "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1"},
    }
    ok, drifted, merged = verify_and_merge_settings(cur, req)
    assert not ok
    assert "includeCoAuthoredBy" in drifted
    assert "attribution" in drifted
    assert "env.CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY" in drifted
    assert merged["model"] == "opus"
    assert merged["includeCoAuthoredBy"] is False
    assert merged["attribution"] == {"commit": "", "pr": ""}


def test_format_buzz_summary():
    assert format_buzz_summary([{"host_label": "ALPHA", "changes": [], "failures": []}]) is None
    line = format_buzz_summary(
        [{"host_label": "ALPHA", "changes": ["Claude 2.1.323→2.1.325", "agy 1.5.3→1.5.9"], "smoke_status": "OK"}]
    )
    assert line == "ALPHA: Claude 2.1.323→2.1.325, agy 1.5.3→1.5.9, smoke OK"
