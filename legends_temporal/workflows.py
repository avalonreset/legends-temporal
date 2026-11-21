# SPDX-License-Identifier: MIT
"""Workflow control and agent-friendly history diagnosis.

`history --diagnose` reduces a full event history to a small verdict: which
activity failed, exit code, last output lines, attempts, worker identity and a
compact timeline. It never dumps raw protobuf JSON into an agent's context.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta
from typing import Any

from .client import decode_payloads, iso, jsonable, seconds

STATUS_ALIASES = {"running": "Running", "completed": "Completed", "failed": "Failed", "canceled": "Canceled",
                  "cancelled": "Canceled", "terminated": "Terminated", "timedout": "TimedOut",
                  "timed_out": "TimedOut", "continuedasnew": "ContinuedAsNew"}


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_query(status: str | None = None, wf_type: str | None = None, id_prefix: str | None = None,
                query: str | None = None) -> str | None:
    clauses = []
    if status:
        key = status.replace("-", "").replace(" ", "").lower()
        if key not in STATUS_ALIASES:
            raise ValueError(f"unknown status '{status}' (use {sorted(set(STATUS_ALIASES.values()))})")
        clauses.append(f"ExecutionStatus={_quote(STATUS_ALIASES[key])}")
    if wf_type:
        clauses.append(f"WorkflowType={_quote(wf_type)}")
    if id_prefix:
        clauses.append(f"WorkflowId STARTS_WITH {_quote(id_prefix)}")
    if query:
        clauses.append(f"({query})")
    return " AND ".join(clauses) or None


def _status(value: Any) -> str | None:
    return getattr(value, "name", None) or (str(value) if value is not None else None)


async def list_workflows(client: Any, query: str | None = None, limit: int = 20) -> dict[str, Any]:
    items = []
    async for wf in client.list_workflows(query=query, limit=limit):
        items.append({"workflow_id": wf.id, "run_id": wf.run_id, "type": wf.workflow_type,
                      "status": _status(wf.status), "task_queue": wf.task_queue,
                      "start_time": iso(wf.start_time), "close_time": iso(wf.close_time),
                      "history_length": wf.history_length})
        if len(items) >= limit:
            break
    return {"ok": True, "query": query, "count": len(items), "workflows": items}


async def describe_workflow(client: Any, workflow_id: str, run_id: str | None = None) -> dict[str, Any]:
    handle = client.get_workflow_handle(workflow_id, run_id=run_id)
    d = await handle.describe()
    try:
        memo = await d.memo()
    except Exception:  # noqa: BLE001
        memo = {}
    pending = []
    for pa in getattr(d.raw_description, "pending_activities", []) or []:
        pending.append({"activity": pa.activity_type.name, "state": pa.State.Name(pa.state) if hasattr(pa, "State") else pa.state,
                        "attempt": pa.attempt, "last_failure": pa.last_failure.message or None,
                        "last_worker": pa.last_worker_identity or None,
                        "scheduled": iso(pa.scheduled_time), "last_started": iso(pa.last_started_time)})
    return {"ok": True, "workflow_id": d.id, "run_id": d.run_id, "type": d.workflow_type,
            "status": _status(d.status), "task_queue": d.task_queue, "start_time": iso(d.start_time),
            "execution_time": iso(d.execution_time), "close_time": iso(d.close_time),
            "history_length": d.history_length, "memo": jsonable(memo), "pending_activities": pending}


# ---------------- history ----------------

def _attr_name(event: Any) -> str:
    name = event.WhichOneof("attributes") or "unknown"
    return name.replace("_event_attributes", "")


def _failure_text(f: Any) -> str | None:
    if f is None or not getattr(f, "message", ""):
        return None
    parts = [f.message]
    c = getattr(f, "cause", None)
    depth = 0
    while c is not None and getattr(c, "message", "") and depth < 4:
        parts.append(c.message)
        c = getattr(c, "cause", None)
        depth += 1
    return " <- ".join(parts)


def _tail_lines(text: str, n: int) -> list[str]:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return lines[-n:] if n > 0 else lines


def summarize_job(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or "job_id" not in payload:
        return None
    cmd = [payload.get("interpreter", ""), payload.get("script", ""), *payload.get("args", [])]
    cmd = [c if len(str(c)) <= 160 else str(c)[:157] + "..." for c in cmd]
    return {"job_id": payload.get("job_id"), "title": payload.get("title"), "exec_queue": payload.get("queue"),
            "command": cmd, "cwd": payload.get("cwd") or None, "timeout_s": payload.get("timeout_s"),
            "notify_channel": payload.get("notify_channel")}


def diagnose_events(events: list[Any], tail_lines: int = 20) -> dict[str, Any]:
    """Pure function over history events (protobuf HistoryEvent objects)."""
    acts: dict[int, dict[str, Any]] = {}
    timeline: list[str] = []
    out: dict[str, Any] = {"status": "RUNNING", "event_count": len(events)}
    t0 = None
    for ev in events:
        kind = _attr_name(ev)
        at = getattr(ev, kind + "_event_attributes")
        ts = iso(ev.event_time)
        if t0 is None:
            t0 = ev.event_time.seconds + ev.event_time.nanos / 1e9
        rel = round(ev.event_time.seconds + ev.event_time.nanos / 1e9 - t0, 1)
        note = ""
        if kind == "workflow_execution_started":
            inputs = decode_payloads(at.input)
            out.update(workflow_type=at.workflow_type.name, task_queue=at.task_queue.name, started_at=ts,
                       attempt=at.attempt, start_delay_s=seconds(at.first_workflow_task_backoff) or 0)
            job = summarize_job(inputs[0]) if inputs else None
            if job:
                out["job"] = job
            elif inputs:
                out["input"] = jsonable(inputs)[:3]
            note = f"{at.workflow_type.name} on {at.task_queue.name}"
        elif kind == "activity_task_scheduled":
            acts[ev.event_id] = {"activity": at.activity_type.name, "queue": at.task_queue.name,
                                 "scheduled_at": ts, "max_attempts": at.retry_policy.maximum_attempts or None,
                                 "outcome": "SCHEDULED"}
            note = f"{at.activity_type.name} -> {at.task_queue.name}"
        elif kind == "activity_task_started":
            a = acts.setdefault(at.scheduled_event_id, {"activity": "?"})
            a.update(attempt=at.attempt, worker=at.identity or None, outcome="STARTED", started_at=ts)
            if at.last_failure and at.last_failure.message:
                a["last_failure"] = _failure_text(at.last_failure)
            note = f"{a['activity']} attempt {at.attempt} by {at.identity}"
        elif kind == "activity_task_completed":
            a = acts.setdefault(at.scheduled_event_id, {"activity": "?"})
            a.update(outcome="COMPLETED", completed_at=ts)
            res = decode_payloads(at.result)
            r = res[0] if res else None
            if isinstance(r, dict) and "exit_code" in r:
                a.update(exit_code=r.get("exit_code"), duration_s=r.get("duration_s"),
                         output_tail=_tail_lines(r.get("tail", ""), tail_lines))
                note = f"{a['activity']} exit={r.get('exit_code')} in {r.get('duration_s')}s"
            else:
                if r is not None:
                    a["result"] = jsonable(r) if len(json.dumps(jsonable(r))) < 400 else "<large result>"
                note = f"{a['activity']} completed"
        elif kind == "activity_task_failed":
            a = acts.setdefault(at.scheduled_event_id, {"activity": "?"})
            a.update(outcome="FAILED", error=_failure_text(at.failure), retry_state=at.retry_state, failed_at=ts)
            note = f"{a['activity']} FAILED: {a['error']}"
        elif kind == "activity_task_timed_out":
            a = acts.setdefault(at.scheduled_event_id, {"activity": "?"})
            a.update(outcome="TIMED_OUT", error=_failure_text(at.failure), failed_at=ts)
            note = f"{a['activity']} TIMED OUT: {a['error']}"
        elif kind == "activity_task_canceled":
            a = acts.setdefault(at.scheduled_event_id, {"activity": "?"})
            a.update(outcome="CANCELED")
        elif kind == "timer_started":
            note = f"timer {seconds(at.start_to_fire_timeout)}s"
        elif kind == "workflow_execution_completed":
            res = decode_payloads(at.result)
            out.update(status="COMPLETED", closed_at=ts, result=jsonable(res[0]) if res else None)
            note = "workflow completed"
        elif kind == "workflow_execution_failed":
            out.update(status="FAILED", closed_at=ts, failure=_failure_text(at.failure))
            note = f"workflow FAILED: {out['failure']}"
        elif kind == "workflow_execution_timed_out":
            out.update(status="TIMED_OUT", closed_at=ts)
            note = "workflow timed out"
        elif kind == "workflow_execution_terminated":
            out.update(status="TERMINATED", closed_at=ts, failure=f"terminated: {at.reason or 'no reason'}")
            note = out["failure"]
        elif kind == "workflow_execution_canceled":
            out.update(status="CANCELED", closed_at=ts)
            note = "workflow canceled"
        elif kind == "workflow_execution_continued_as_new":
            out.update(status="CONTINUED_AS_NEW", closed_at=ts, new_run_id=at.new_execution_run_id)
        elif kind == "workflow_execution_signaled":
            note = f"signal {at.signal_name}"
        elif kind.startswith("workflow_task"):
            if kind == "workflow_task_failed":
                note = f"workflow task FAILED: {_failure_text(at.failure)}"
                out.setdefault("workflow_task_failures", []).append(_failure_text(at.failure))
            else:
                continue
        if note:
            timeline.append(f"+{rel}s #{ev.event_id} {kind}: {note}")
    activities = list(acts.values())
    out["activities"] = activities
    out["timeline"] = timeline[-40:]
    out["verdict"] = _verdict(out, activities)
    return out


def _verdict(out: dict[str, Any], activities: list[dict[str, Any]]) -> dict[str, Any]:
    v: dict[str, Any] = {"ok": out["status"] == "COMPLETED", "status": out["status"]}
    script = [a for a in activities if a.get("activity") == "run_script"]
    main = script[-1] if script else None
    if main:
        v["script_exit_code"] = main.get("exit_code")
        v["script_attempts"] = main.get("attempt")
        v["script_worker"] = main.get("worker")
        if main.get("outcome") in {"FAILED", "TIMED_OUT"}:
            v["failed_activity"] = "run_script"
            v["error"] = main.get("error")
        elif main.get("exit_code") not in (None, 0):
            v["failed_activity"] = "run_script"
            v["error"] = f"script exited {main.get('exit_code')}"
        elif main.get("outcome") == "SCHEDULED":
            v["hint"] = f"run_script waiting for a worker on {main.get('queue')} (check `queue describe`)"
        if main.get("output_tail") is not None:
            v["output_tail"] = main["output_tail"]
    else:
        failed = [a for a in activities if a.get("outcome") in {"FAILED", "TIMED_OUT"}]
        if failed:
            v["failed_activity"] = failed[-1].get("activity")
            v["error"] = failed[-1].get("error")
    notices = [a for a in activities if a.get("activity") == "notify_buzz"]
    if notices:
        v["notice"] = "posted" if notices[-1].get("outcome") == "COMPLETED" else notices[-1].get("outcome", "").lower()
    logs = [a for a in activities if a.get("activity") == "log_failure"]
    if logs:
        v["failure_logged"] = logs[-1].get("outcome") == "COMPLETED"
    if out.get("failure"):
        v["workflow_failure"] = out["failure"]
    if out.get("workflow_task_failures"):
        v["hint"] = "workflow task failures: likely non-determinism or a worker code error"
    return v


async def fetch_events(client: Any, workflow_id: str, run_id: str | None = None) -> list[Any]:
    handle = client.get_workflow_handle(workflow_id, run_id=run_id)
    return [e async for e in handle.fetch_history_events()]


async def history(client: Any, workflow_id: str, run_id: str | None = None, diagnose: bool = False,
                  limit: int = 60, tail_lines: int = 20) -> dict[str, Any]:
    events = await fetch_events(client, workflow_id, run_id)
    if diagnose:
        d = diagnose_events(events, tail_lines=tail_lines)
        return {"ok": True, "workflow_id": workflow_id, "run_id": run_id, **d}
    rows = []
    for ev in events[-limit:]:
        rows.append({"id": ev.event_id, "time": iso(ev.event_time), "type": _attr_name(ev)})
    return {"ok": True, "workflow_id": workflow_id, "event_count": len(events), "shown": len(rows), "events": rows}


# ---------------- control ----------------

def parse_input(raw: str | None) -> list[Any]:
    if raw is None or raw == "":
        return []
    val = json.loads(raw)
    return val if isinstance(val, list) else [val]


async def start(client: Any, wf_type: str, workflow_id: str, task_queue: str, args: list[Any],
                start_delay_s: int = 0, execution_timeout_s: int | None = None, wait: bool = False,
                wait_timeout_s: float | None = None) -> dict[str, Any]:
    kw: dict[str, Any] = {"id": workflow_id, "task_queue": task_queue}
    if start_delay_s:
        kw["start_delay"] = timedelta(seconds=start_delay_s)
    if execution_timeout_s:
        kw["execution_timeout"] = timedelta(seconds=execution_timeout_s)
    handle = await client.start_workflow(wf_type, args=args, **kw)
    out: dict[str, Any] = {"ok": True, "workflow_id": handle.id, "run_id": handle.result_run_id, "type": wf_type}
    if wait:
        out.update(await wait_result(handle, wait_timeout_s))
    return out


async def wait_result(handle: Any, timeout_s: float | None) -> dict[str, Any]:
    from temporalio.client import WorkflowFailureError
    try:
        res = await asyncio.wait_for(handle.result(), timeout=timeout_s)
        return {"status": "COMPLETED", "result": jsonable(res)}
    except WorkflowFailureError as exc:
        return {"ok": False, "status": "FAILED", "failure": str(exc.cause or exc)}
    except asyncio.TimeoutError:
        return {"ok": False, "status": "WAIT_TIMEOUT"}


async def signal(client: Any, workflow_id: str, name: str, args: list[Any], run_id: str | None = None):
    await client.get_workflow_handle(workflow_id, run_id=run_id).signal(name, args=args)
    return {"ok": True, "workflow_id": workflow_id, "signal": name}


async def query(client: Any, workflow_id: str, name: str, args: list[Any], run_id: str | None = None):
    res = await client.get_workflow_handle(workflow_id, run_id=run_id).query(name, args=args)
    return {"ok": True, "workflow_id": workflow_id, "query": name, "result": jsonable(res)}


async def cancel(client: Any, workflow_id: str, run_id: str | None = None):
    await client.get_workflow_handle(workflow_id, run_id=run_id).cancel()
    return {"ok": True, "workflow_id": workflow_id, "action": "cancel-requested"}


async def terminate(client: Any, workflow_id: str, reason: str, run_id: str | None = None):
    await client.get_workflow_handle(workflow_id, run_id=run_id).terminate(reason=reason)
    return {"ok": True, "workflow_id": workflow_id, "action": "terminated", "reason": reason}


async def run_job(client: Any, workflow_queue: str, wire: dict[str, Any], wait: bool = True,
                  timeout_s: int = 900) -> dict[str, Any]:
    """Run one ScriptJob now (canary / ad-hoc) under a unique id `<job_id>-<epoch>`."""
    wid = f"{wire['job_id']}-{int(time.time())}"
    handle = await client.start_workflow("ScriptJob", wire, id=wid, task_queue=workflow_queue,
                                         execution_timeout=timedelta(seconds=timeout_s))
    out: dict[str, Any] = {"ok": True, "workflow_id": wid, "run_id": handle.result_run_id, "job_id": wire["job_id"]}
    if wait:
        out.update(await wait_result(handle, timeout_s + 30))
    return out
