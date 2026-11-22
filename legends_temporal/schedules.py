# SPDX-License-Identifier: MIT
"""Full Schedule lifecycle: list, describe, create/apply, trigger, pause, unpause, delete, backfill.

Declarative job definitions (JSON, YAML when PyYAML is present, or a Python
`module:ATTR` inventory) contain the ScriptJob wire fields plus schedule keys:

  cron | crons | interval (e.g. "15m") [interval_offset], timezone, jitter,
  overlap (SKIP|BUFFER_ONE|BUFFER_ALL|CANCEL_OTHER|TERMINATE_OTHER|ALLOW_ALL),
  catchup_window (e.g. "12h"), pause_on_failure, remaining_actions, paused, note
"""
from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import runtime
from .client import decode_payloads, iso, jsonable, seconds
from .config import Profile
from .timers import parse_delay

OVERLAPS = ("SKIP", "BUFFER_ONE", "BUFFER_ALL", "CANCEL_OTHER", "TERMINATE_OTHER", "ALLOW_ALL")
SCHEDULE_KEYS = {"cron", "crons", "interval", "interval_offset", "timezone", "jitter", "overlap",
                 "catchup_window", "pause_on_failure", "remaining_actions", "paused", "note", "kind",
                 "session_id", "spawn", "agent", "effort", "task_type", "project", "workspace",
                 "message", "model", "code", "module", "agent_ctl", "description"}


# ---------------- declarative definitions ----------------

def load_definitions(path: str | None = None, python_source: str | None = None,
                     pythonpath: list[str] | None = None) -> list[dict[str, Any]]:
    """Load job definitions -> list of dicts (wire fields + schedule keys)."""
    if python_source:
        return load_python_jobs(python_source, pythonpath or [])
    if not path:
        raise ValueError("give a definitions file (--file) or --from-python module:ATTR")
    p = Path(path)
    text = p.read_text(encoding="utf-8-sig")
    if p.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise ValueError("YAML definitions need PyYAML (pip install pyyaml) or use JSON") from exc
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("jobs", data.get("schedules", [data]))
    if isinstance(data, dict):
        data = [dict(v, job_id=v.get("job_id", k)) for k, v in data.items()]
    if not isinstance(data, list):
        raise ValueError("definitions must be a list of jobs or {\"jobs\": [...]}")
    return [dict(d) for d in data]


def load_python_jobs(source: str, pythonpath: list[str]) -> list[dict[str, Any]]:
    """`package.module:ATTR` where ATTR is a dict or list of ScriptJob-like objects."""
    mod_name, _, attr = source.partition(":")
    if not mod_name or not attr:
        raise ValueError("python source must be module:ATTR, e.g. legends_ops.jobs:JOBS")
    for entry in reversed(pythonpath):
        if entry and entry not in sys.path:
            sys.path.insert(0, entry)
    obj = getattr(importlib.import_module(mod_name), attr)
    items = obj.values() if isinstance(obj, dict) else obj
    out = []
    for item in items:
        d = runtime.to_wire(item)
        rem = runtime.remaining_actions_of(item)
        if rem:
            d["remaining_actions"] = rem
        out.append(d)
    return out


def expand_definition(defn: dict[str, Any], profile: Profile) -> dict[str, Any]:
    """Fill agent/code shorthands and profile defaults; returns definition with valid wire fields."""
    d = dict(defn)
    legacy_ctl = "omni" + "gent_ctl"
    if legacy_ctl in d:
        d.setdefault("agent_ctl", d.pop(legacy_ctl))
    kind = d.get("kind", "script")
    d.setdefault("queue", profile.exec_queue)
    d.setdefault("interpreter", profile.interpreter)
    d.setdefault("cwd", profile.cwd)
    d.setdefault("notify_channel", profile.notify_channel)
    d.setdefault("title", d.get("job_id", ""))
    if kind == "agent":
        is_spawn = bool(d.get("spawn") or (d.get("agent") and not d.get("session_id")))
        job = runtime.agent_job(d["job_id"], d["title"], d["queue"], d["interpreter"],
                                d.get("agent_ctl") or profile.agent_ctl, d.get("session_id"), d["message"],
                                d.get("model"), cwd=d["cwd"], notify_channel=d["notify_channel"],
                                timeout_s=int(d.get("timeout_s", 120)), cron=d.get("cron") or "",
                                wait_for_worker_s=int(d.get("wait_for_worker_s", 6 * 3600)),
                                spawn=is_spawn, agent=d.get("agent"), effort=d.get("effort"),
                                task_type=d.get("task_type") or "build", project=d.get("project"),
                                workspace=d.get("workspace") or d["cwd"],
                                excluded_agents=profile.excluded_agents, sessions=profile.sessions)
        d.update(runtime.to_wire(job))
    elif kind == "code":
        d["script"], d["args"] = "-c", [d["code"], *d.get("args", [])]
    elif kind == "module" or d.get("module"):
        job = runtime.module_job(
            d["job_id"], d["title"], d["queue"], d["interpreter"], d["module"],
            d.get("args"), cwd=d["cwd"], notify_channel=d["notify_channel"],
            timeout_s=int(d.get("timeout_s", 900)), cron=d.get("cron") or "",
            wait_for_worker_s=int(d.get("wait_for_worker_s", 6 * 3600)),
        )
        d.update(runtime.to_wire(job))
    if isinstance(d.get("cron"), str) and d.get("cron"):
        d.setdefault("crons", [d["cron"]])
    return d


def split_definition(d: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    wire = runtime.to_wire({k: v for k, v in d.items() if k in runtime.WIRE_FIELDS})
    sched = {k: v for k, v in d.items() if k in SCHEDULE_KEYS}
    unknown = set(d) - set(runtime.WIRE_FIELDS) - SCHEDULE_KEYS
    if unknown:
        raise ValueError(f"job {d.get('job_id')}: unknown keys {sorted(unknown)}")
    return wire, sched


def build_schedule(wire: dict[str, Any], sched: dict[str, Any], profile: Profile):
    from temporalio.client import (Schedule, ScheduleActionStartWorkflow, ScheduleIntervalSpec,
                                   ScheduleOverlapPolicy, SchedulePolicy, ScheduleSpec, ScheduleState)

    crons = list(sched.get("crons") or ([wire["cron"]] if wire.get("cron") else []))
    intervals = []
    if sched.get("interval"):
        intervals.append(ScheduleIntervalSpec(
            every=timedelta(seconds=parse_delay(str(sched["interval"]))),
            offset=timedelta(seconds=parse_delay(str(sched["interval_offset"]))) if sched.get("interval_offset") else None))
    if not crons and not intervals:
        raise ValueError(f"job {wire['job_id']}: needs cron or interval")
    overlap = str(sched.get("overlap", "SKIP")).upper()
    if overlap not in OVERLAPS:
        raise ValueError(f"overlap must be one of {OVERLAPS}")
    rem = int(sched.get("remaining_actions") or 0)
    state = ScheduleState(note=sched.get("note") or wire["title"], paused=bool(sched.get("paused", False)),
                          limited_actions=rem > 0, remaining_actions=rem if rem > 0 else 0)
    return Schedule(
        action=ScheduleActionStartWorkflow("ScriptJob", wire, id=wire["job_id"], task_queue=profile.workflow_queue,
                                           execution_timeout=runtime.schedule_execution_timeout(wire)),
        spec=ScheduleSpec(cron_expressions=crons, intervals=intervals,
                          time_zone_name=sched.get("timezone") or profile.timezone,
                          jitter=timedelta(seconds=parse_delay(str(sched["jitter"]))) if sched.get("jitter") else None),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy[overlap],
                              catchup_window=timedelta(seconds=parse_delay(str(sched.get("catchup_window", "12h")))),
                              pause_on_failure=bool(sched.get("pause_on_failure", False))),
        state=state,
    )


def plan_summary(wire: dict[str, Any], sched: dict[str, Any], profile: Profile) -> dict[str, Any]:
    return {"id": wire["job_id"], "title": wire["title"], "crons": sched.get("crons") or [],
            "interval": sched.get("interval"), "timezone": sched.get("timezone") or profile.timezone,
            "overlap": str(sched.get("overlap", "SKIP")).upper(), "catchup_window": sched.get("catchup_window", "12h"),
            "remaining_actions": int(sched.get("remaining_actions") or 0) or None,
            "workflow_queue": profile.workflow_queue, "exec_queue": wire["queue"],
            "command": [wire["interpreter"], wire["script"], *wire["args"]]}


async def apply_definitions(client: Any, profile: Profile, defs: list[dict[str, Any]],
                            dry_run: bool = False, only: list[str] | None = None) -> dict[str, Any]:
    from temporalio.client import ScheduleAlreadyRunningError, ScheduleUpdate

    results = []
    for raw in defs:
        d = expand_definition(raw, profile)
        if only and d["job_id"] not in only:
            continue
        wire, sched = split_definition(d)
        if not sched.get("crons") and wire.get("cron"):
            sched["crons"] = [wire["cron"]]
        summary = plan_summary(wire, sched, profile)
        schedule = build_schedule(wire, sched, profile)
        if dry_run:
            results.append({**summary, "action": "would-apply"})
            continue
        try:
            await client.create_schedule(wire["job_id"], schedule, memo={"title": wire["title"]})
            results.append({**summary, "action": "created"})
        except ScheduleAlreadyRunningError:
            handle = client.get_schedule_handle(wire["job_id"])
            await handle.update(lambda _inp, s=schedule: ScheduleUpdate(schedule=s))
            results.append({**summary, "action": "updated"})
    return {"ok": True, "dry_run": dry_run, "count": len(results), "schedules": results}


# ---------------- rendering ----------------

def _ranges(rs: Any) -> str:
    parts = []
    for r in rs or []:
        start, end, step = r.start, (r.end or r.start), (r.step or 1)
        txt = str(start) if end == start else f"{start}-{end}"
        if step != 1:
            txt += f"/{step}"
        parts.append(txt)
    return ",".join(parts) or "*"


_FULL_RANGES = {"minute": "0-59", "hour": "0-23", "day_of_month": "1-31", "month": "1-12", "day_of_week": "0-6"}


def calendar_to_cron(cal: Any) -> str:
    parts = []
    for f in ("minute", "hour", "day_of_month", "month", "day_of_week"):
        text = _ranges(getattr(cal, f))
        full = _FULL_RANGES[f]
        if text == full:
            parts.append("*")
        elif text.startswith(full + "/"):
            parts.append("*" + text[len(full):])
        else:
            parts.append(text)
    return " ".join(parts)


def spec_dict(spec: Any) -> dict[str, Any]:
    if spec is None:
        return {}
    out: dict[str, Any] = {
        "crons": list(spec.cron_expressions or []),
        "calendars": [calendar_to_cron(c) for c in (spec.calendars or [])],
        "intervals": [{"every_s": seconds(i.every), "offset_s": seconds(i.offset)} for i in (spec.intervals or [])],
        "timezone": spec.time_zone_name or "UTC",
    }
    if spec.jitter:
        out["jitter_s"] = seconds(spec.jitter)
    if spec.start_at:
        out["start_at"] = iso(spec.start_at)
    if spec.end_at:
        out["end_at"] = iso(spec.end_at)
    return out


def _recent(actions: Any, limit: int = 5) -> list[dict[str, Any]]:
    out = []
    for a in list(actions or [])[-limit:]:
        act = getattr(a, "action", None)
        out.append({"scheduled_at": iso(getattr(a, "scheduled_at", None)),
                    "started_at": iso(getattr(a, "started_at", None)),
                    "workflow_id": getattr(act, "workflow_id", None),
                    "run_id": getattr(act, "first_execution_run_id", None)})
    return out


def action_dict(action: Any, decode_args: bool = True) -> dict[str, Any]:
    if action is None:
        return {}
    out: dict[str, Any] = {"workflow_type": getattr(action, "workflow", None)}
    for attr in ("id", "task_queue"):
        if getattr(action, attr, None):
            out[attr] = getattr(action, attr)
    if getattr(action, "execution_timeout", None):
        out["execution_timeout_s"] = seconds(action.execution_timeout)
    args = getattr(action, "args", None)
    if decode_args and args:
        decoded = []
        for a in args:
            decoded.extend(decode_payloads([a]) if hasattr(a, "data") and hasattr(a, "metadata") else [jsonable(a)])
        out["args"] = decoded
    return out


async def list_schedules(client: Any, query: str | None = None, limit: int = 200) -> dict[str, Any]:
    items = []
    async for s in await client.list_schedules(query=query):
        sch, info = s.schedule, s.info
        state = getattr(sch, "state", None)
        items.append({
            "id": s.id,
            "workflow_type": getattr(getattr(sch, "action", None), "workflow", None),
            "spec": spec_dict(getattr(sch, "spec", None)),
            "paused": bool(getattr(state, "paused", False)),
            "note": getattr(state, "note", "") or "",
            "next_run": iso(info.next_action_times[0]) if info and info.next_action_times else None,
            "recent_actions": _recent(getattr(info, "recent_actions", None), 3),
        })
        if len(items) >= limit:
            break
    items.sort(key=lambda x: x["id"])
    return {"ok": True, "count": len(items), "schedules": items}


async def describe_schedule(client: Any, schedule_id: str) -> dict[str, Any]:
    handle = client.get_schedule_handle(schedule_id)
    desc = await handle.describe()
    sch, info = desc.schedule, desc.info
    try:
        memo = await desc.memo()
    except Exception:  # noqa: BLE001
        memo = {}
    st, pol = sch.state, sch.policy
    return {
        "ok": True,
        "id": desc.id,
        "memo": jsonable(memo),
        "action": action_dict(sch.action),
        "spec": spec_dict(sch.spec),
        "policy": {"overlap": getattr(pol.overlap, "name", str(pol.overlap)),
                   "catchup_window_s": seconds(pol.catchup_window),
                   "pause_on_failure": bool(pol.pause_on_failure)},
        "state": {"paused": bool(st.paused), "note": st.note or "", "limited_actions": bool(st.limited_actions),
                  "remaining_actions": st.remaining_actions if st.limited_actions else None},
        "info": {"num_actions": info.num_actions,
                 "missed_catchup_window": info.num_actions_missed_catchup_window,
                 "skipped_overlap": info.num_actions_skipped_overlap,
                 "running": [{"workflow_id": r.workflow_id, "run_id": r.first_execution_run_id}
                             for r in info.running_actions],
                 "recent_actions": _recent(info.recent_actions, 5),
                 "next_runs": [iso(t) for t in info.next_action_times[:5]],
                 "created_at": iso(info.created_at), "last_updated_at": iso(info.last_updated_at)},
    }


async def trigger(client: Any, schedule_id: str, overlap: str = "ALLOW_ALL") -> dict[str, Any]:
    from temporalio.client import ScheduleOverlapPolicy
    await client.get_schedule_handle(schedule_id).trigger(overlap=ScheduleOverlapPolicy[overlap.upper()])
    return {"ok": True, "id": schedule_id, "action": "triggered", "overlap": overlap.upper()}


async def pause(client: Any, schedule_id: str, note: str | None = None) -> dict[str, Any]:
    await client.get_schedule_handle(schedule_id).pause(note=note or "paused by legends-temporal")
    return {"ok": True, "id": schedule_id, "action": "paused", "note": note}


async def unpause(client: Any, schedule_id: str, note: str | None = None) -> dict[str, Any]:
    await client.get_schedule_handle(schedule_id).unpause(note=note or "unpaused by legends-temporal")
    return {"ok": True, "id": schedule_id, "action": "unpaused", "note": note}


async def delete(client: Any, schedule_id: str) -> dict[str, Any]:
    await client.get_schedule_handle(schedule_id).delete()
    return {"ok": True, "id": schedule_id, "action": "deleted"}


async def backfill(client: Any, schedule_id: str, start: datetime, end: datetime,
                   overlap: str = "ALLOW_ALL") -> dict[str, Any]:
    from temporalio.client import ScheduleBackfill, ScheduleOverlapPolicy
    if end <= start:
        raise ValueError("--end must be after --start")
    await client.get_schedule_handle(schedule_id).backfill(
        ScheduleBackfill(start_at=start, end_at=end, overlap=ScheduleOverlapPolicy[overlap.upper()]))
    return {"ok": True, "id": schedule_id, "action": "backfilled", "start": iso(start), "end": iso(end)}


def flag_definition(args: Any, profile: Profile) -> dict[str, Any]:
    """Build one definition dict from `schedule create` flags."""
    d: dict[str, Any] = {"job_id": args.id, "title": args.title or args.id}
    if getattr(args, "spawn", False) or (getattr(args, "agent", None) and not args.session):
        if not args.message:
            raise ValueError("--spawn / --agent requires --message")
        d.update(
            kind="agent",
            spawn=True,
            agent=getattr(args, "agent", None) or "auto",
            message=args.message,
            model=args.model or "auto",
            effort=getattr(args, "effort", None),
            task_type=getattr(args, "task_type", None) or "build",
            project=getattr(args, "project", None),
            workspace=getattr(args, "workspace", None) or args.cwd or profile.cwd,
        )
    elif args.session:
        d.update(kind="agent", session_id=args.session, message=args.message, model=args.model)
        if not args.message:
            raise ValueError("--session requires --message")
    elif args.code:
        d.update(kind="code", code=args.code, args=list(args.args or []))
    elif getattr(args, "module", None):
        d.update(kind="module", module=args.module, args=list(args.args or []))
    elif args.script:
        d.update(script=args.script, args=list(args.args or []))
    else:
        raise ValueError("choose one of --script, --code, --module, --session, --spawn/--agent")
    if args.cron:
        d["crons"] = list(args.cron)
    if args.interval:
        d["interval"] = args.interval
    for key in ("timezone", "overlap", "catchup_window", "jitter", "queue", "interpreter", "cwd", "notify_channel"):
        val = getattr(args, key, None)
        if val:
            d[key] = val
    if args.remaining_actions:
        d["remaining_actions"] = args.remaining_actions
    if args.timeout_s:
        d["timeout_s"] = args.timeout_s
    if args.paused:
        d["paused"] = True
    return d
