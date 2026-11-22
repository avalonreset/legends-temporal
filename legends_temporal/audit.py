# SPDX-License-Identifier: MIT
"""Schedule health, poller availability, secret hygiene, and declarative drift auditing.

`legends-temporal audit` operates in two modes:
  1. Offline inventory audit (`--offline` with `--file` or `--from-python`):
     Validates 5-field cron/interval syntax, IANA timezones, 11-field ScriptJob wire
     payloads, overlap vs. timeout pile-up hazards, and scans arguments/code for
     raw secrets or fragile shell-sleep antipatterns before touching a cluster.
  2. Live cluster audit (default):
     Inspects all live schedules on the Temporal server, cross-checks every target
     workflow_queue and exec_queue against live task-queue pollers (catching
     orphaned queues with 0 active workers), flags paused schedules or missed
     catchup windows, and when an inventory (`--file`, `--from-python`, or profile
     `jobs_source`) is provided, computes a 3-way drift report (`in_sync`,
     `missing_on_server`, `untracked_on_server`, `spec_drifted`).
"""
from __future__ import annotations

import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import queues, runtime, schedules
from .config import Profile
from .timers import parse_delay

SECRET_ARG_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("RAW_WEBHOOK_URL", re.compile(r"https?://(?:hooks\.slack\.com|discord(?:app)?\.com/api/webhooks|api\.telegram\.org/bot)", re.I)),
    ("API_KEY_TOKEN", re.compile(r"(?:sk-[a-zA-Z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|Bearer\s+[A-Za-z0-9._-]{20,})")),
    ("INLINE_PASSWORD_ASSIGN", re.compile(r"(?i)(?:api[_-]?key|secret|password|token)\s*=\s*['\"][^'\"]{8,}['\"]")),
    ("SHELL_SLEEP_LOOP", re.compile(r"(?i)\b(?:nohup\s+.*?\bsleep\s+\d+|while\s+true\s*;\s*do\s+sleep)\b")),
)

CRON_FIELD_RE = re.compile(r"^[\d*,/-]+$")


def _validate_cron_expr(expr: str) -> str | None:
    parts = (expr or "").strip().split()
    if len(parts) != 5:
        return f"cron '{expr}' must have 5 fields (minute hour day-of-month month day-of-week)"
    for field in parts:
        if not CRON_FIELD_RE.match(field):
            return f"cron '{expr}' contains invalid field '{field}'"
    return None


def _validate_timezone(tz_name: str) -> str | None:
    if not tz_name or tz_name == "UTC":
        return None
    try:
        ZoneInfo(tz_name)
        return None
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return f"unknown IANA timezone '{tz_name}'"


def _scan_secrets_and_antipatterns(wire: dict[str, Any]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    haystack = [str(wire.get("script", "")), *[str(a) for a in (wire.get("args") or [])]]
    joined = " ".join(haystack)
    for code, pat in SECRET_ARG_PATTERNS:
        if pat.search(joined):
            findings.append({
                "severity": "ERROR" if code != "SHELL_SLEEP_LOOP" else "WARN",
                "code": code,
                "job_id": wire.get("job_id", "?"),
                "message": (
                    f"Job '{wire.get('job_id')}' embeds {code} in workflow arguments; "
                    "keep secrets in worker environment variables (e.g. --webhook-env) so they never enter Temporal history."
                    if code != "SHELL_SLEEP_LOOP"
                    else f"Job '{wire.get('job_id')}' uses a shell sleep loop; use Temporal cron/interval schedules or timer start_delay instead."
                ),
            })
    return findings


def audit_definitions(defs: list[dict[str, Any]], profile: Profile) -> dict[str, Any]:
    """Offline static audit of declarative schedule definitions."""
    issues: list[dict[str, str]] = []
    audited: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for raw in defs:
        jid = str(raw.get("job_id") or raw.get("id") or "?")
        if jid in seen_ids:
            issues.append({
                "severity": "ERROR",
                "code": "DUPLICATE_JOB_ID",
                "job_id": jid,
                "message": f"Duplicate job_id '{jid}' in schedule inventory.",
            })
        seen_ids.add(jid)
        try:
            expanded = schedules.expand_definition(raw, profile)
            wire, sched = schedules.split_definition(expanded)
            if not sched.get("crons") and wire.get("cron"):
                sched["crons"] = [wire["cron"]]
            _ = schedules.build_schedule(wire, sched, profile)
        except Exception as exc:  # noqa: BLE001
            issues.append({
                "severity": "ERROR",
                "code": "INVALID_DEFINITION",
                "job_id": jid,
                "message": str(exc),
            })
            continue

        crons = list(sched.get("crons") or [])
        for c in crons:
            err = _validate_cron_expr(c)
            if err:
                issues.append({"severity": "ERROR", "code": "INVALID_CRON", "job_id": jid, "message": err})

        tz = str(sched.get("timezone") or profile.timezone or "UTC")
        tz_err = _validate_timezone(tz)
        if tz_err:
            issues.append({"severity": "ERROR", "code": "INVALID_TIMEZONE", "job_id": jid, "message": tz_err})

        overlap = str(sched.get("overlap", "SKIP")).upper()
        timeout_s = int(wire.get("timeout_s", 900))
        if sched.get("interval"):
            interval_s = parse_delay(str(sched["interval"]))
            if timeout_s >= interval_s and overlap in {"ALLOW_ALL", "BUFFER_ALL"}:
                issues.append({
                    "severity": "WARN",
                    "code": "OVERLAP_PILEUP_RISK",
                    "job_id": jid,
                    "message": (
                        f"Job '{jid}' has timeout_s={timeout_s}s >= interval={interval_s}s with overlap={overlap}; "
                        "use SKIP or BUFFER_ONE to prevent concurrent queue pile-ups."
                    ),
                })

        issues.extend(_scan_secrets_and_antipatterns(wire))
        audited.append({
            "job_id": wire["job_id"],
            "title": wire["title"],
            "crons": crons,
            "interval": sched.get("interval"),
            "timezone": tz,
            "overlap": overlap,
            "workflow_queue": profile.workflow_queue,
            "exec_queue": wire["queue"],
            "timeout_s": timeout_s,
            "wire": wire,
        })

    errors = [i for i in issues if i["severity"] == "ERROR"]
    warnings = [i for i in issues if i["severity"] == "WARN"]
    return {
        "ok": len(errors) == 0,
        "mode": "offline",
        "profile": profile.name,
        "checked_count": len(audited),
        "error_count": len(errors),
        "warning_count": len(warnings),
        "issues": issues,
        "schedules": [
            {k: v for k, v in item.items() if k != "wire"}
            for item in audited
        ],
        "_audited_by_id": {item["job_id"]: item for item in audited},
    }


def _normalize_cron_set(crons: list[str], calendars: list[str]) -> list[str]:
    merged = {c.strip() for c in (crons or []) + (calendars or []) if c and c.strip()}
    return sorted(merged)


async def audit_cluster(
    client: Any,
    profile: Profile,
    defs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Live cluster schedule audit: queue poller cross-check, paused/missed ticks, and optional inventory drift."""
    issues: list[dict[str, str]] = []
    offline_map: dict[str, dict[str, Any]] = {}
    if defs is not None:
        off = audit_definitions(defs, profile)
        issues.extend(off["issues"])
        offline_map = off["_audited_by_id"]

    live_list = await schedules.list_schedules(client, limit=500)
    live_rows: list[dict[str, Any]] = []
    referenced_queues: set[str] = {profile.workflow_queue, profile.exec_queue, *(profile.watch_queues or [])}

    detailed_by_id: dict[str, dict[str, Any]] = {}
    for row in live_list["schedules"]:
        sid = row["id"]
        desc = await schedules.describe_schedule(client, sid)
        detailed_by_id[sid] = desc
        act = desc.get("action") or {}
        wq = act.get("task_queue") or profile.workflow_queue
        if wq:
            referenced_queues.add(wq)
        args = act.get("args") or []
        first_arg = args[0] if args and isinstance(args[0], dict) else {}
        eq = first_arg.get("queue") or wq
        if eq:
            referenced_queues.add(eq)
        if first_arg:
            issues.extend(_scan_secrets_and_antipatterns(first_arg))

    queue_health: dict[str, dict[str, Any]] = {}
    for qname in sorted(referenced_queues):
        if not qname:
            continue
        qd = await queues.describe_queue(client, profile.namespace, qname)
        wf_live = sum(1 for p in qd.get("workflow_pollers", []) if p.get("live"))
        act_live = sum(1 for p in qd.get("activity_pollers", []) if p.get("live"))
        queue_health[qname] = {
            "healthy": qd["healthy"],
            "live_pollers": qd["live_pollers"],
            "workflow_pollers_live": wf_live,
            "activity_pollers_live": act_live,
        }

    for sid, desc in sorted(detailed_by_id.items()):
        act = desc.get("action") or {}
        spec = desc.get("spec") or {}
        state = desc.get("state") or {}
        info = desc.get("info") or {}
        pol = desc.get("policy") or {}
        args = act.get("args") or []
        first_arg = args[0] if args and isinstance(args[0], dict) else {}
        wq = act.get("task_queue") or profile.workflow_queue
        eq = first_arg.get("queue") or wq

        wq_info = queue_health.get(wq, {})
        eq_info = queue_health.get(eq, {})
        if wq_info.get("workflow_pollers_live", 0) == 0:
            issues.append({
                "severity": "ERROR",
                "code": "ORPHANED_WORKFLOW_QUEUE",
                "job_id": sid,
                "message": f"Schedule '{sid}' targets workflow_queue '{wq}' which has 0 live workflow pollers.",
            })
        if eq_info.get("activity_pollers_live", 0) == 0:
            issues.append({
                "severity": "ERROR",
                "code": "ORPHANED_EXEC_QUEUE",
                "job_id": sid,
                "message": f"Schedule '{sid}' dispatches activities to exec_queue '{eq}' which has 0 live activity pollers.",
            })
        if state.get("paused"):
            issues.append({
                "severity": "WARN",
                "code": "SCHEDULE_PAUSED",
                "job_id": sid,
                "message": f"Schedule '{sid}' is currently PAUSED (note: {state.get('note') or 'none'}).",
            })
        missed = int(info.get("missed_catchup_window") or 0)
        if missed > 0:
            issues.append({
                "severity": "WARN",
                "code": "MISSED_CATCHUP_WINDOW",
                "job_id": sid,
                "message": f"Schedule '{sid}' has {missed} action(s) that missed their catchup window.",
            })

        live_rows.append({
            "id": sid,
            "paused": bool(state.get("paused")),
            "workflow_queue": wq,
            "exec_queue": eq,
            "crons": _normalize_cron_set(spec.get("crons", []), spec.get("calendars", [])),
            "timezone": spec.get("timezone", "UTC"),
            "overlap": pol.get("overlap"),
            "next_run": (info.get("next_runs") or [None])[0],
            "missed_catchup_window": missed,
            "skipped_overlap": int(info.get("skipped_overlap") or 0),
        })

    drift: dict[str, Any] | None = None
    if defs is not None:
        in_sync: list[str] = []
        missing_on_server: list[str] = []
        untracked_on_server: list[str] = []
        spec_drifted: list[dict[str, Any]] = []

        for jid, expected in sorted(offline_map.items()):
            if jid not in detailed_by_id:
                missing_on_server.append(jid)
                issues.append({
                    "severity": "WARN",
                    "code": "MISSING_ON_SERVER",
                    "job_id": jid,
                    "message": f"Inventory schedule '{jid}' is not registered on the Temporal server.",
                })
                continue
            desc = detailed_by_id[jid]
            spec = desc.get("spec") or {}
            act = desc.get("action") or {}
            args = act.get("args") or []
            first_arg = args[0] if args and isinstance(args[0], dict) else {}
            live_crons = _normalize_cron_set(spec.get("crons", []), spec.get("calendars", []))
            exp_crons = _normalize_cron_set(expected["crons"], [])
            live_eq = first_arg.get("queue") or act.get("task_queue") or profile.exec_queue
            diffs: list[str] = []
            if exp_crons and live_crons and exp_crons != live_crons:
                diffs.append(f"crons: inventory={exp_crons} vs server={live_crons}")
            if expected["timezone"] != (spec.get("timezone") or "UTC"):
                diffs.append(f"timezone: inventory={expected['timezone']} vs server={spec.get('timezone') or 'UTC'}")
            if expected["exec_queue"] != live_eq:
                diffs.append(f"exec_queue: inventory={expected['exec_queue']} vs server={live_eq}")
            if diffs:
                spec_drifted.append({"job_id": jid, "differences": diffs})
                issues.append({
                    "severity": "WARN",
                    "code": "SPEC_DRIFTED",
                    "job_id": jid,
                    "message": f"Schedule '{jid}' drifted from inventory ({'; '.join(diffs)}).",
                })
            else:
                in_sync.append(jid)

        for sid in sorted(detailed_by_id):
            if sid not in offline_map:
                untracked_on_server.append(sid)

        drift = {
            "in_sync": in_sync,
            "missing_on_server": missing_on_server,
            "untracked_on_server": untracked_on_server,
            "spec_drifted": spec_drifted,
        }

    errors = [i for i in issues if i["severity"] == "ERROR"]
    warnings = [i for i in issues if i["severity"] == "WARN"]
    out: dict[str, Any] = {
        "ok": len(errors) == 0,
        "mode": "live",
        "profile": profile.name,
        "namespace": profile.namespace,
        "schedule_count": len(live_rows),
        "error_count": len(errors),
        "warning_count": len(warnings),
        "queues": queue_health,
        "issues": issues,
        "schedules": live_rows,
    }
    if drift is not None:
        out["drift"] = drift
    return out
