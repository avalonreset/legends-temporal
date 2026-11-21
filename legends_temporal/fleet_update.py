# SPDX-License-Identifier: MIT
"""Generic, config-driven fleet auto-update engine for AI CLIs and desktop harnesses.

Supports:
  - Read-only discovery across local, Temporal worker queues, and SSH hosts
  - Vendor latest-version lookups (HTTP text, HTTP JSON, HTTP regex, CLI command, check-only)
  - Idle gate via session listing (/v1/sessions) with 2+ consecutive idle-check window hunting
    and 24-hour busy timeout notification
  - Desktop app idle gate (closed or >=30m OS user input idle overnight)
  - Canary rollout order with per-subscription smoke tests and automatic rollback on failure
  - Post-update settings restoration and patch verification/re-application
  - Markdown version table rendering and single-line notification formatting
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def parse_version_tuple(ver: str | None) -> tuple[int, ...]:
    """Parse version strings like '2.1.325', '1.5.9', '5.11492.5', '1.4.20-R8619.1' into integer tuples."""
    if not ver:
        return ()
    cleaned = ver.strip()
    if cleaned.lower() in {"n/a", "none", "not_installed", "unknown", "check-only", "-"}:
        return ()
    nums = [int(n) for n in re.findall(r"\d+", cleaned)]
    return tuple(nums)


def is_update_available(installed: str | None, latest: str | None, *, check_only: bool = False) -> bool:
    """Return True only when both versions are valid and installed < latest."""
    if check_only or not installed or not latest:
        return False
    iv = parse_version_tuple(installed)
    lv = parse_version_tuple(latest)
    if not iv or not lv:
        return False
    return iv < lv


def fetch_latest_version(spec: dict[str, Any], installed: str | None = None, timeout_s: int = 15) -> str:
    """Query the vendor's latest version using a declarative read-only spec."""
    kind = str(spec.get("type") or "check_only").lower()
    if kind == "check_only":
        return installed or "check-only"
    headers = {"User-Agent": "legends-temporal-fleet-update/1.0"}
    headers.update(spec.get("headers") or {})
    url = str(spec.get("url") or "")
    if installed and "{installed}" in url:
        url = url.replace("{installed}", installed)
    try:
        if kind == "http_text":
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return resp.read().decode("utf-8", errors="replace").strip().splitlines()[0].strip()
        if kind == "http_json":
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                doc = json.loads(resp.read().decode("utf-8", errors="replace"))
            key_path = str(spec.get("json_key") or "version").split(".")
            cur: Any = doc
            for k in key_path:
                if isinstance(cur, dict):
                    cur = cur.get(k)
            return str(cur or "").strip()
        if kind == "http_regex":
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            pattern = str(spec.get("pattern") or r"([0-9]+\.[0-9]+(?:\.[0-9]+)+)")
            matches = re.findall(pattern, body)
            if not matches:
                return installed or "unknown"
            # Pick highest version among matches in Squirrel RELEASES
            best = max(matches, key=parse_version_tuple)
            return str(best).strip()
        if kind == "command":
            cmd = spec.get("command")
            if not cmd:
                return installed or "unknown"
            proc = subprocess.run(
                cmd,
                shell=isinstance(cmd, str),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
            )
            out = (proc.stdout or "") + "\n" + (proc.stderr or "")
            pattern = str(spec.get("pattern") or r"^\s*([0-9]+\.[0-9]+(?:\.[0-9]+)+)\s*$")
            matches = re.findall(pattern, out, flags=re.MULTILINE)
            if matches:
                best = max(matches, key=parse_version_tuple)
                return str(best).strip()
            return installed or "unknown"
    except Exception:
        if spec.get("fallback_command"):
            try:
                proc = subprocess.run(
                    spec["fallback_command"],
                    shell=isinstance(spec["fallback_command"], str),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_s,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    return proc.stdout.strip().splitlines()[-1].strip()
            except Exception:
                pass
        return installed or "unknown"
    return installed or "unknown"


def get_windows_input_idle_seconds() -> float | None:
    """Return seconds since last keyboard/mouse input on Windows, or None on POSIX/error."""
    if os.name != "nt":
        return None

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    try:
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            tick = ctypes.windll.kernel32.GetTickCount()
            elapsed_ms = (tick - lii.dwTime) & 0xFFFFFFFF
            return round(elapsed_ms / 1000.0, 1)
    except Exception:
        return None
    return None


TERMINAL_SESSION_STATUSES = {"idle", "failed", "completed", "cancelled", "stopped", "done", "archived"}


def find_busy_sessions(
    sessions: list[dict[str, Any]],
    *,
    host_id: str | None = None,
    harness: str | None = None,
    all_harnesses_on_host: bool = False,
) -> list[dict[str, Any]]:
    """Return active (non-idle, non-archived) sessions matching host_id and harness."""
    busy: list[dict[str, Any]] = []
    for s in sessions:
        if s.get("archived"):
            continue
        status = str(s.get("status") or "").strip().lower()
        if status in TERMINAL_SESSION_STATUSES:
            continue
        if host_id and str(s.get("host_id") or "") != host_id:
            continue
        if not all_harnesses_on_host and harness:
            sh = str(s.get("harness") or "").strip().lower()
            if sh != harness.lower():
                continue
        busy.append(
            {
                "id": s.get("id") or s.get("session_id"),
                "title": s.get("title"),
                "status": s.get("status"),
                "harness": s.get("harness"),
                "host_id": s.get("host_id"),
            }
        )
    return busy


@dataclass
class GateDecision:
    allowed: bool
    state: str  # "ready", "pending_busy", "pending_streak", "pending_desktop_active", "expired_24h"
    reason: str
    consecutive_idle_checks: int = 0
    pending_seconds: float = 0.0
    busy_sessions: list[dict[str, Any]] = field(default_factory=list)


def evaluate_tool_gate(
    *,
    host_key: str,
    tool_key: str,
    host_id: str | None,
    harness: str | None,
    sessions: list[dict[str, Any]],
    hunt_state: dict[str, Any],
    now_epoch: float | None = None,
    required_idle_checks: int = 2,
    max_pending_s: int = 86400,
    is_desktop: bool = False,
    desktop_process_running: bool = False,
    input_idle_s: float | None = None,
    min_desktop_idle_s: int = 1800,
    is_overnight: bool = True,
    requires_daemon_restart: bool = False,
) -> GateDecision:
    """Evaluate the session and desktop idle gates and update window-hunting state in place."""
    now = now_epoch if now_epoch is not None else time.time()
    state_key = f"{host_key}:{tool_key}"
    entry = dict(hunt_state.get(state_key) or {})
    pending_since = float(entry.get("pending_since") or now)
    streak = int(entry.get("consecutive_idle_checks") or 0)

    if is_desktop:
        if desktop_process_running:
            if not is_overnight or (input_idle_s is not None and input_idle_s < min_desktop_idle_s):
                entry.update(
                    {
                        "pending_since": pending_since,
                        "consecutive_idle_checks": 0,
                        "last_checked": now,
                        "last_reason": f"desktop app open and user input idle {input_idle_s}s < {min_desktop_idle_s}s",
                    }
                )
                hunt_state[state_key] = entry
                elapsed = now - pending_since
                if elapsed >= max_pending_s:
                    return GateDecision(False, "expired_24h", "pending 24 h: always busy", 0, elapsed)
                return GateDecision(
                    False,
                    "pending_desktop_active",
                    f"desktop app open (input idle {input_idle_s}s < {min_desktop_idle_s}s)",
                    0,
                    elapsed,
                )

    busy = find_busy_sessions(
        sessions,
        host_id=host_id,
        harness=harness,
        all_harnesses_on_host=requires_daemon_restart,
    )
    if busy:
        entry.update(
            {
                "pending_since": pending_since,
                "consecutive_idle_checks": 0,
                "last_checked": now,
                "last_busy_ids": [b.get("id") for b in busy],
            }
        )
        hunt_state[state_key] = entry
        elapsed = now - pending_since
        if elapsed >= max_pending_s:
            return GateDecision(False, "expired_24h", "pending 24 h: always busy", 0, elapsed, busy)
        return GateDecision(
            False,
            "pending_busy",
            f"{len(busy)} active session(s) on {harness or 'host'}",
            0,
            elapsed,
            busy,
        )

    streak += 1
    entry.update(
        {
            "pending_since": pending_since,
            "consecutive_idle_checks": streak,
            "last_checked": now,
            "last_busy_ids": [],
        }
    )
    if streak < required_idle_checks:
        hunt_state[state_key] = entry
        elapsed = now - pending_since
        return GateDecision(
            False,
            "pending_streak",
            f"idle check {streak}/{required_idle_checks} (waiting for next window confirmation)",
            streak,
            elapsed,
        )

    # Gate passed! Clear hunt state for this host:tool
    hunt_state.pop(state_key, None)
    return GateDecision(
        True,
        "ready",
        f"idle ({streak}/{required_idle_checks} consecutive checks passed)",
        streak,
        now - pending_since,
    )


def verify_and_merge_settings(
    current: dict[str, Any] | None,
    required: dict[str, Any],
) -> tuple[bool, list[str], dict[str, Any]]:
    """Check whether `current` contains all `required` settings; return (ok, drifted_keys, merged_doc)."""
    base = json.loads(json.dumps(current)) if isinstance(current, dict) else {}
    drifted: list[str] = []

    def _walk(target: dict[str, Any], req: dict[str, Any], prefix: str = "") -> None:
        for k, v in req.items():
            path = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                sub = target.get(k)
                if not isinstance(sub, dict):
                    drifted.append(path)
                    target[k] = json.loads(json.dumps(v))
                else:
                    _walk(sub, v, path)
            else:
                if target.get(k) != v:
                    drifted.append(path)
                    target[k] = v

    _walk(base, required)
    return (len(drifted) == 0), drifted, base


def format_buzz_summary(host_events: list[dict[str, Any]]) -> str | None:
    """Format a single-line summary when something changed, expired, or failed; return None when quiet."""
    segments: list[str] = []
    for ev in host_events:
        host_label = str(ev.get("host_label") or ev.get("host") or "").upper()
        changes = list(ev.get("changes") or [])
        failures = list(ev.get("failures") or [])
        expired = list(ev.get("expired") or [])
        smoke_status = ev.get("smoke_status")
        parts: list[str] = []
        parts.extend(changes)
        parts.extend(failures)
        parts.extend(expired)
        if not parts:
            continue
        if smoke_status:
            parts.append(f"smoke {smoke_status}")
        segments.append(f"{host_label}: {', '.join(parts)}")
    if not segments:
        return None
    return " · ".join(segments)


def render_fleet_versions_md(
    report: dict[str, Any],
    changelog_lines: list[str] | None = None,
) -> str:
    """Render the canonical Fleet-Versions.md markdown table and dated change log."""
    ts = report.get("generated_at") or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    mode = report.get("mode") or "discover"
    lines = [
        "# Fleet Tool Versions & Auto-Update Status",
        "",
        f"Last checked: `{ts}` · Mode: `{mode}` · Schedule: `ops-fleet-update` (`04:00 ET` daily, canary-first).",
        "",
        "## 1. Host × Tool Version & Update Matrix",
        "",
        "| Host | Tool | Installed | Latest Vendor | Status | Idle Gate | Update Command | Rollback Option |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in report.get("rows") or []:
        lines.append(
            f"| **{row.get('host_label')}** | {row.get('tool_label')} | `{row.get('installed')}` | "
            f"`{row.get('latest')}` | {row.get('status')} | {row.get('gate')} | "
            f"`{row.get('update_cmd')}` | {row.get('rollback_desc')} |"
        )

    lines.extend(
        [
            "",
            "## 2. House Settings & Patch Integrity",
            "",
            "| Host | Check | Status | Details |",
            "|---|---|---|---|",
        ]
    )
    for item in report.get("integrity") or []:
        lines.append(
            f"| **{item.get('host_label')}** | `{item.get('name')}` | {item.get('status')} | {item.get('detail')} |"
        )

    lines.extend(
        [
            "",
            "## 3. Dated Change Log",
            "",
        ]
    )
    for entry in changelog_lines or [f"- `{ts[:10]}`: Initial read-only discovery & dry-run baseline recorded."]:
        lines.append(entry)
    lines.append("")
    return "\n".join(lines)
