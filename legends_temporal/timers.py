# SPDX-License-Identifier: MIT
"""One-shot durable delayed timers (`timer in <delay>`), built on Temporal start_delay.

Modes (exactly one):
  --script PATH [--args ...]          run a script on the exec worker
  --code "..."                        inline Python (-c) on the exec worker
  --session ID --message "..."        wake an agent session via agent_ctl.py send
  --command ARGV...                   any argv (shell=False) via the worker's Python
  --webhook-env VAR [--body JSON]     POST JSON to the URL held in env VAR on the worker
"""
from __future__ import annotations

import re
import time
from datetime import timedelta
from typing import Any

from . import runtime
from .config import Profile

_UNITS = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
          "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
          "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
          "d": 86400, "day": 86400, "days": 86400, "w": 604800, "week": 604800, "weeks": 604800}
MAX_DELAY_S = 366 * 86400


def parse_delay(spec: str) -> int:
    """'30s', '15m', '2h', '3d', '1h30m', '90' (seconds) -> integer seconds."""
    s = (spec or "").strip().lower().replace(" ", "")
    if not s:
        raise ValueError("empty delay")
    if s.isdigit():
        total = int(s)
    else:
        if not re.fullmatch(r"(\d+[a-z]+)+", s):
            raise ValueError(f"invalid delay '{spec}' (use e.g. 30s, 15m, 2h, 3d, 1h30m)")
        total = 0
        for val, unit in re.findall(r"(\d+)([a-z]+)", s):
            if unit not in _UNITS:
                raise ValueError(f"unknown unit '{unit}' in '{spec}'")
            total += int(val) * _UNITS[unit]
    if total < 0 or total > MAX_DELAY_S:
        raise ValueError(f"delay must be between 0s and {MAX_DELAY_S}s")
    return total


def build_timer_job(profile: Profile, delay_label: str, *, script: str | None = None, code: str | None = None,
                    session: str | None = None, spawn: bool = False, agent: str | None = None,
                    message: str | None = None, model: str | None = None, effort: str | None = None,
                    task_type: str = "build", project: str | None = None, workspace: str = "",
                    command: list[str] | None = None, webhook_env: str | None = None,
                    body: dict[str, Any] | None = None, args: list[str] | None = None, cwd: str = "",
                    title: str = "", notify_channel: str | None = None, queue: str | None = None,
                    interpreter: str | None = None, agent_ctl: str | None = None,
                    timeout_s: int | None = None, wait_for_worker_s: int = 6 * 3600,
                    job_id: str | None = None, now: int | None = None) -> runtime.ScriptJob:
    is_spawn = bool(spawn or (agent and not session))
    modes = [m for m, v in (("script", script), ("code", code), ("spawn", is_spawn), ("session", session),
                            ("command", command), ("webhook", webhook_env)) if v]
    if len(modes) != 1:
        raise ValueError("choose exactly one of --script, --code, --session, --spawn/--agent, --command, --webhook-env")
    mode = modes[0]
    ts = int(time.time()) if now is None else now
    q = queue or profile.exec_queue
    interp = interpreter or profile.interpreter
    chan = notify_channel or profile.notify_channel
    wd = workspace or cwd or profile.cwd
    common = dict(cwd=wd, notify_channel=chan, wait_for_worker_s=wait_for_worker_s)
    if mode == "spawn":
        if not message:
            raise ValueError("--spawn / --agent requires --message")
        agent_label = (agent or "auto").strip()
        slug = re.sub(r"[^a-z0-9]+", "-", agent_label.lower()).strip("-") or "auto"
        return runtime.agent_job(
            job_id or f"oneshot-spawn-{slug}-{ts}",
            title or f"oneshot · spawn {agent_label} (+{delay_label})",
            q, interp, agent_ctl or profile.agent_ctl, None, message, model or "auto",
            timeout_s=timeout_s or 120, spawn=True, agent=agent_label, effort=effort,
            task_type=task_type or "build", project=project, workspace=wd,
            excluded_agents=profile.excluded_agents, sessions=profile.sessions, **common)
    if mode == "session":
        if not message:
            raise ValueError("--session requires --message")
        resolved_session = (profile.sessions or {}).get(session, session)
        return runtime.agent_job(job_id or f"oneshot-session-{resolved_session[:8]}-{ts}",
                                 title or f"oneshot · wake session {resolved_session[:8]} (+{delay_label})",
                                 q, interp, agent_ctl or profile.agent_ctl, resolved_session, message, model,
                                 timeout_s=timeout_s or 120, excluded_agents=profile.excluded_agents,
                                 sessions=profile.sessions, **common)
    if mode == "code":
        return runtime.code_job(job_id or f"oneshot-code-{ts}", title or f"oneshot · inline python (+{delay_label})",
                                q, interp, code, args, timeout_s=timeout_s or 900, **common)
    if mode == "script":
        name = script.replace("\\", "/").rsplit("/", 1)[-1]
        return runtime.script_job(job_id or f"oneshot-script-{ts}", title or f"oneshot · {name} (+{delay_label})",
                                  q, interp, script, args, timeout_s=timeout_s or 900, **common)
    if mode == "command":
        return runtime.command_job(job_id or f"oneshot-command-{ts}",
                                   title or f"oneshot · {command[0]} (+{delay_label})", q, interp, command,
                                   timeout_s=timeout_s or 900, **common)
    return runtime.webhook_job(job_id or f"oneshot-webhook-{ts}", title or f"oneshot · webhook (+{delay_label})",
                               q, interp, webhook_env, body or {}, timeout_s=timeout_s or 120, **common)


async def start_timer(client: Any, profile: Profile, job: runtime.ScriptJob, delay_s: int,
                      wait: bool = False, wait_timeout_s: float | None = None) -> dict[str, Any]:
    wire = runtime.to_wire(job)
    handle = await client.start_workflow(
        "ScriptJob", wire, id=wire["job_id"], task_queue=profile.workflow_queue,
        start_delay=timedelta(seconds=delay_s), execution_timeout=runtime.timer_execution_timeout(wire, delay_s),
        memo={"title": wire["title"], "created_by": "legends-temporal timer"})
    out: dict[str, Any] = {"ok": True, "workflow_id": handle.id, "run_id": handle.result_run_id,
                           "fires_in_s": delay_s, "workflow_queue": profile.workflow_queue,
                           "exec_queue": wire["queue"], "title": wire["title"]}
    if wait:
        import asyncio

        from temporalio.client import WorkflowFailureError
        try:
            res = await asyncio.wait_for(handle.result(), timeout=wait_timeout_s)
            out.update(status="COMPLETED", result=res)
        except WorkflowFailureError as exc:
            out.update(ok=False, status="FAILED", failure=str(exc.cause or exc))
        except asyncio.TimeoutError:
            out.update(ok=False, status="WAIT_TIMEOUT", note="still running or pending; check workflow describe")
    return out
