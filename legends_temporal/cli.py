# SPDX-License-Identifier: MIT
"""legends-temporal command line. See docs/CLI-CONTRACT.json for the exact contract."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .client import (EXIT_ERROR, EXIT_NOT_FOUND, EXIT_OK, EXIT_USAGE, EXIT_WORKFLOW_FAILED,
                     LegendsTemporalError, cluster_safe, connect, dumps)
from .config import ConfigError, Profile, config_path, resolve_profile

PORTABLE_TEMPLATE = {
    "default_profile": "local",
    "profiles": {
        "local": {
            "description": "Local dev server (temporal server start-dev)",
            "address": "localhost:7233", "namespace": "default",
            "workflow_queue": "legends-temporal", "exec_queue": "legends-temporal",
            "interpreter": "python", "timezone": "UTC", "notify_channel": "ops",
            "ui_url": "http://localhost:8233", "notify": {"kind": "stdout"},
        }
    },
}


# ---------------- argument parsing ----------------

def _common(sup: bool) -> argparse.ArgumentParser:
    d = argparse.SUPPRESS if sup else None
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--profile", default=d, help="config profile name")
    p.add_argument("--config", default=d, help="config file path")
    p.add_argument("--address", default=d, help="host:port (overrides profile and TEMPORAL_ADDRESS)")
    p.add_argument("--namespace", default=d, help="namespace (overrides profile and TEMPORAL_NAMESPACE)")
    p.add_argument("--timeout", type=float, default=argparse.SUPPRESS if sup else 10.0,
                   help="connect timeout seconds (default 10)")
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS if sup else False,
                   help="machine-readable JSON output")
    return p


def build_parser() -> argparse.ArgumentParser:
    common = _common(sup=True)
    ap = argparse.ArgumentParser(prog="legends-temporal", parents=[_common(sup=False)],
                                 description="Agent-operated control of any Temporal cluster.")
    ap.add_argument("--version", action="version", version=f"legends-temporal {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(parent, name, help_text):
        return parent.add_parser(name, help=help_text, parents=[common])

    add(sub, "version", "print version JSON")
    add(sub, "agents", "preflight subscription agents, models and live auth")
    p = add(sub, "doctor", "offline readiness; --live adds cluster checks")
    p.add_argument("--live", action="store_true")
    add(sub, "resources", "absolute paths of recipe, contract, knowledge pack and skill")

    cfg = sub.add_parser("config", help="show or initialize configuration").add_subparsers(dest="sub", required=True)
    add(cfg, "show", "print the resolved profile (no secrets)")
    p = add(cfg, "init", "write a portable config template (refuses to overwrite)")
    p.add_argument("--path", default=None)

    ns = sub.add_parser("namespace", help="namespace inspection").add_subparsers(dest="sub", required=True)
    add(ns, "list", "list namespaces")
    p = add(ns, "describe", "describe a namespace plus cluster info")
    p.add_argument("name", nargs="?")

    q = sub.add_parser("queue", help="task queue inspection").add_subparsers(dest="sub", required=True)
    p = add(q, "describe", "pollers on workflow and activity task queues")
    p.add_argument("task_queues", nargs="*", help="queue names (default: profile watch_queues)")

    s = sub.add_parser("schedule", help="schedule lifecycle").add_subparsers(dest="sub", required=True)
    p = add(s, "list", "list schedules")
    p.add_argument("--query", default=None)
    p = add(s, "describe", "describe a schedule")
    p.add_argument("schedule_id")
    p = add(s, "create", "create or update one ScriptJob schedule from flags")
    p.add_argument("--id", required=True)
    p.add_argument("--title", default="")
    p.add_argument("--cron", action="append", help="5-field cron (repeatable)")
    p.add_argument("--interval", help="e.g. 15m, 1h")
    p.add_argument("--timezone", default=None)
    p.add_argument("--overlap", default=None, help="SKIP|BUFFER_ONE|BUFFER_ALL|CANCEL_OTHER|TERMINATE_OTHER|ALLOW_ALL")
    p.add_argument("--catchup-window", dest="catchup_window", default=None)
    p.add_argument("--jitter", default=None)
    p.add_argument("--remaining-actions", dest="remaining_actions", type=int, default=0)
    p.add_argument("--paused", action="store_true")
    _job_flags(p, title=False)
    p.add_argument("--dry-run", action="store_true")
    p = add(s, "apply", "create/update schedules from a definitions file or Python inventory")
    p.add_argument("--file", default=None, help="JSON or YAML definitions")
    p.add_argument("--from-python", dest="from_python", default=None, help="module:ATTR (default: profile jobs_source)")
    p.add_argument("--pythonpath", action="append", default=None)
    p.add_argument("--only", action="append", default=None, help="apply only these job ids")
    p.add_argument("--dry-run", action="store_true")
    p = add(s, "trigger", "run a schedule's action now")
    p.add_argument("schedule_id")
    p.add_argument("--overlap", default="ALLOW_ALL")
    for name in ("pause", "unpause"):
        p = add(s, name, f"{name} a schedule")
        p.add_argument("schedule_id")
        p.add_argument("--note", default=None)
    p = add(s, "delete", "delete a schedule (needs --yes)")
    p.add_argument("schedule_id")
    p.add_argument("--yes", action="store_true")
    p = add(s, "backfill", "run actions for a past window")
    p.add_argument("schedule_id")
    p.add_argument("--start", required=True, help="ISO-8601, e.g. 2026-11-15T08:00:00Z")
    p.add_argument("--end", required=True)
    p.add_argument("--overlap", default="ALLOW_ALL")

    t = sub.add_parser("timer", help="one-shot durable delayed timers").add_subparsers(dest="sub", required=True)
    p = add(t, "in", "fire a job after a delay (30s, 15m, 2h, 3d, 1h30m)")
    p.add_argument("delay")
    p.add_argument("--command", nargs=argparse.REMAINDER, default=None,
                   help="argv to run (must be the last option)")
    p.add_argument("--webhook-env", dest="webhook_env", default=None, help="env var on the worker holding the URL")
    p.add_argument("--body", default=None, help="JSON body for --webhook-env")
    p.add_argument("--job-id", dest="job_id", default=None)
    p.add_argument("--wait", action="store_true")
    p.add_argument("--wait-timeout", dest="wait_timeout", type=float, default=None)
    p.add_argument("--dry-run", action="store_true")
    _job_flags(p)

    w = sub.add_parser("workflow", help="workflow control and diagnosis").add_subparsers(dest="sub", required=True)
    p = add(w, "list", "list executions")
    p.add_argument("--status", default=None)
    p.add_argument("--type", dest="wf_type", default=None)
    p.add_argument("--id-prefix", dest="id_prefix", default=None)
    p.add_argument("--query", default=None)
    p.add_argument("--limit", type=int, default=20)
    p = add(w, "describe", "describe an execution")
    p.add_argument("workflow_id")
    p.add_argument("--run-id", dest="run_id", default=None)
    p = add(w, "history", "event history; --diagnose for a compact verdict")
    p.add_argument("workflow_id")
    p.add_argument("--run-id", dest="run_id", default=None)
    p.add_argument("--diagnose", action="store_true")
    p.add_argument("--limit", type=int, default=60)
    p.add_argument("--tail-lines", dest="tail_lines", type=int, default=20)
    p = add(w, "start", "start a workflow")
    p.add_argument("wf_type")
    p.add_argument("--id", required=True)
    p.add_argument("--queue", default=None, help="task queue (default: profile workflow_queue)")
    p.add_argument("--input", default=None, help="JSON value or JSON list of args")
    p.add_argument("--start-delay", dest="start_delay", default=None)
    p.add_argument("--execution-timeout", dest="execution_timeout", default=None)
    p.add_argument("--wait", action="store_true")
    p.add_argument("--wait-timeout", dest="wait_timeout", type=float, default=None)
    for name in ("signal", "query"):
        p = add(w, name, f"{name} a workflow")
        p.add_argument("workflow_id")
        p.add_argument("name")
        p.add_argument("--input", default=None)
        p.add_argument("--run-id", dest="run_id", default=None)
    p = add(w, "cancel", "request cancellation")
    p.add_argument("workflow_id")
    p.add_argument("--run-id", dest="run_id", default=None)
    p = add(w, "terminate", "terminate immediately (needs --yes)")
    p.add_argument("workflow_id")
    p.add_argument("--reason", required=True)
    p.add_argument("--yes", action="store_true")
    p.add_argument("--run-id", dest="run_id", default=None)

    j = sub.add_parser("job", help="Python job inventories (JOBS / CANARIES)").add_subparsers(dest="sub", required=True)
    for name in ("list", "run"):
        p = add(j, name, "list inventory jobs offline" if name == "list" else "run one inventory job now")
        if name == "run":
            p.add_argument("job_id")
            p.add_argument("--no-wait", dest="no_wait", action="store_true")
            p.add_argument("--run-timeout", dest="run_timeout", type=int, default=900)
        p.add_argument("--from-python", dest="from_python", default=None)
        p.add_argument("--pythonpath", action="append", default=None)
        p.add_argument("--canary", action="store_true", help="use profile canaries_source")

    p = add(sub, "worker", "run a ScriptJob worker")
    p.add_argument("--role", choices=["control", "exec", "all"], default="all")
    p.add_argument("--log-file", dest="log_file", default=None)
    p.add_argument("--max-concurrent", dest="max_concurrent", type=int, default=2)
    add(sub, "mcp", "serve the MCP stdio JSON-RPC server")
    return ap


def _job_flags(p: argparse.ArgumentParser, title: bool = True) -> None:
    p.add_argument("--script", default=None)
    p.add_argument("--code", default=None)
    p.add_argument("--args", nargs="*", default=[], help="script arguments (values must not start with '-')")
    p.add_argument("--arg", dest="arg_extra", action="append", default=[],
                   help="one script argument, repeatable; use --arg=--flag for dash values (appended after --args)")
    p.add_argument("--session", default=None, help="agent session id to wake")
    p.add_argument("--spawn", action="store_true", help="spawn a fresh agent session")
    p.add_argument("--agent", default=None, help="subscription agent for --spawn: claude|gemini|muse|codex|auto")
    p.add_argument("--message", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--effort", default=None, help="reasoning effort: low|medium|high|xhigh|max|auto")
    p.add_argument("--type", dest="task_type", default="build",
                   help="Stipend Router task type when model='auto': hard|build|bulk|audit")
    p.add_argument("--project", default=None, help="agent project name, slug, or ID for --spawn")
    p.add_argument("--workspace", default="", help="workspace directory for --spawn")
    p.add_argument("--cwd", default="")
    if title:
        p.add_argument("--title", default="")
    p.add_argument("--notify-channel", dest="notify_channel", default=None)
    p.add_argument("--queue", default=None, help="exec queue (default: profile exec_queue)")
    p.add_argument("--interpreter", default=None)
    p.add_argument("--timeout-s", dest="timeout_s", type=int, default=None)


# ---------------- human rendering ----------------

def _human(cmd: str, sub: str | None, r: dict[str, Any]) -> str:
    key = f"{cmd} {sub}" if sub else cmd
    if key == "schedule list":
        lines = [f"{len(r['schedules'])} schedules"]
        for s in r["schedules"]:
            spec = s["spec"]
            when = ",".join(spec.get("crons") or spec.get("calendars") or
                            [f"every {i['every_s']}s" for i in spec.get("intervals", [])])
            lines.append(f"  {s['id']:<34} {when:<18} {spec.get('timezone',''):<18} "
                         f"{'PAUSED ' if s['paused'] else ''}next={s['next_run'] or '-'}")
        return "\n".join(lines)
    if key == "workflow list":
        lines = [f"{r['count']} workflows" + (f" (query: {r['query']})" if r.get("query") else "")]
        for w in r["workflows"]:
            lines.append(f"  {w['status'] or '?':<11} {w['workflow_id']:<44} {w['type']:<12} {w['start_time']}")
        return "\n".join(lines)
    if key == "queue describe":
        lines = []
        for q in r["queues"]:
            lines.append(f"{q['task_queue']}: {'HEALTHY' if q['healthy'] else 'NO LIVE POLLERS'} "
                         f"({q['live_pollers']} live)")
            for kind in ("workflow_pollers", "activity_pollers"):
                for p in q[kind]:
                    lines.append(f"  {kind.split('_')[0]:<8} {p['identity']:<44} age={p['age_s']}s "
                                 f"rate={p['rate_per_second']}")
        return "\n".join(lines)
    if key == "namespace list":
        return "\n".join([f"{r['count']} namespaces"] + [f"  {n['name']:<24} {n['state']:<11} "
                         f"retention={n['retention_days']}d" for n in r["namespaces"]])
    if key == "agents":
        lines = [
            f"Host: {r.get('host_name')} ({r.get('host_id')}) · status={r.get('host_status')} · origin={r.get('origin')}",
            "",
        ]
        agents = r.get("agents") or {}
        for alias in ("claude", "gemini", "muse", "codex"):
            info = agents.get(alias) or {}
            avail = "AVAILABLE" if info.get("available") else "UNAVAILABLE"
            lines.append(f"[{avail}] {alias} · id={info.get('agent_id')} · harness={info.get('harness')}")
            lines.append(f"  Subscription: {info.get('label')}")
            lines.append(f"  Auth status : {info.get('auth_status')}")
            if info.get("stipend"):
                st = info["stipend"]
                lines.append(
                    f"  Stipend     : pace={st.get('pace')} · weekly_left={st.get('weekly_remaining_pct')}% "
                    f"· 5h_left={st.get('five_hour_remaining_pct')}% · usable={st.get('usable')}"
                )
            efforts = ", ".join(info.get("supported_efforts") or [])
            models = ", ".join(info.get("models") or [])
            lines.append(f"  Efforts     : {efforts}")
            lines.append(f"  Models      : {models or '(none)'}  [{info.get('model_source')}]")
            if info.get("one_click_remediation"):
                lines.append(f"  One-click   : {info.get('one_click_remediation')}")
            lines.append("")
        for k, reason in (r.get("excluded") or {}).items():
            lines.append(f"[EXCLUDED] {k} · {reason}")
        picks = r.get("stipend_auto_picks") or {}
        if picks:
            lines.append("")
            lines.append("Stipend Router model='auto' current picks:")
            for t, pk in picks.items():
                lines.append(
                    f"  --type {t:<5} -> agent={pk.get('pool')}  model={pk.get('model')}  "
                    f"effort={pk.get('effort')}  ({pk.get('band')}: {pk.get('reason')})"
                )
        return "\n".join(lines)
    if key == "doctor":
        lines = [f"legends-temporal {r['version']} doctor ({'live' if r['live'] else 'offline'}) "
                 f"profile={r['profile']}: {'OK' if r['ok'] else 'FAIL'}"]
        for c in r["checks"]:
            detail = c["detail"] if isinstance(c["detail"], str) else json.dumps(c["detail"], default=str)
            lines.append(f"  [{c['status']}] {c['name']}: {detail[:160]}")
        return "\n".join(lines)
    if key == "workflow history" and "verdict" in r:
        v = r["verdict"]
        lines = [f"{r['workflow_id']}: {v['status']}" + (f" | failed: {v.get('failed_activity')}" if
                                                          v.get("failed_activity") else "")]
        for k in ("error", "script_exit_code", "script_attempts", "script_worker", "notice", "failure_logged", "hint"):
            if v.get(k) is not None:
                lines.append(f"  {k}: {v[k]}")
        if v.get("output_tail"):
            lines.append("  output tail:")
            lines += [f"    {ln}" for ln in v["output_tail"]]
        lines.append("  timeline:")
        lines += [f"    {ln}" for ln in r.get("timeline", [])]
        return "\n".join(lines)
    return dumps(r)


# ---------------- command execution ----------------

def _parse_dt(text: str) -> datetime:
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _inventory(a: argparse.Namespace, profile: Profile) -> list[dict[str, Any]]:
    from .schedules import load_definitions
    src = a.from_python or (profile.canaries_source if getattr(a, "canary", False) else profile.jobs_source)
    if not src:
        raise ConfigError("no Python inventory: pass --from-python module:ATTR or set profile jobs_source")
    return load_definitions(python_source=src, pythonpath=a.pythonpath or profile.jobs_pythonpath)


async def _dispatch(a: argparse.Namespace, profile: Profile) -> dict[str, Any]:
    from . import doctor, namespaces, queues, runtime, schedules, timers, workflows

    cmd, sub = a.cmd, getattr(a, "sub", None)
    if cmd == "version":
        return {"ok": True, "tool": "legends-temporal", "version": __version__}
    if cmd == "agents":
        import subprocess
        ctl = profile.agent_ctl
        if not ctl:
            raise ConfigError("agents preflight needs profile.agent_ctl")
        interp = profile.interpreter if (profile.interpreter and Path(profile.interpreter).exists()) else sys.executable
        proc = subprocess.run([interp, ctl, "agents"], capture_output=True, text=True, timeout=30, check=False,
                              creationflags=runtime.subprocess_flags())
        if proc.returncode != 0:
            raise LegendsTemporalError("AGENTS_PREFLIGHT_FAILED",
                                       (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip(), EXIT_ERROR)
        return json.loads(proc.stdout)
    if cmd == "doctor":
        return await doctor.run_doctor(profile, live=a.live, config=a.config, timeout_s=a.timeout)
    if cmd == "resources":
        return {"ok": True, "root": str(doctor.ROOT), "paths": doctor.resource_paths()}
    if cmd == "config":
        if sub == "show":
            return {"ok": True, "config_path": str(config_path(a.config)), "profile": profile.public_dict()}
        path = Path(a.path).expanduser() if a.path else config_path(a.config)
        if path.exists():
            raise ConfigError(f"{path} exists; refusing to overwrite")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(PORTABLE_TEMPLATE, indent=2) + "\n", encoding="utf-8")
        return {"ok": True, "written": str(path)}
    if cmd == "job" and sub == "list":
        defs = _inventory(a, profile)
        return {"ok": True, "count": len(defs), "jobs": [
            {"job_id": d["job_id"], "title": d["title"], "cron": d.get("cron") or None, "queue": d["queue"],
             "remaining_actions": d.get("remaining_actions")} for d in defs]}
    if cmd == "schedule" and sub in {"create", "apply"} and a.dry_run:
        defs = [schedules.flag_definition(a, profile)] if sub == "create" else (
            schedules.load_definitions(a.file, None, None) if a.file else _inventory(a, profile))
        return await schedules.apply_definitions(None, profile, defs, dry_run=True,
                                                 only=getattr(a, "only", None))
    if cmd == "timer":
        delay_s = timers.parse_delay(a.delay)
        body = json.loads(a.body) if a.body else None
        job = timers.build_timer_job(profile, a.delay, script=a.script, code=a.code, session=a.session,
                                     spawn=a.spawn, agent=a.agent, message=a.message, model=a.model,
                                     effort=a.effort, task_type=a.task_type, project=a.project,
                                     workspace=a.workspace, command=a.command,
                                     webhook_env=a.webhook_env, body=body, args=a.args, cwd=a.cwd, title=a.title,
                                     notify_channel=a.notify_channel, queue=a.queue, interpreter=a.interpreter,
                                     timeout_s=a.timeout_s, job_id=a.job_id)
        if a.dry_run:
            return {"ok": True, "dry_run": True, "fires_in_s": delay_s, "workflow_queue": profile.workflow_queue,
                    "job": runtime.to_wire(job)}
    if cmd == "schedule" and sub == "delete" and not a.yes:
        raise ConfigError("schedule delete needs --yes")
    if cmd == "workflow" and sub == "terminate" and not a.yes:
        raise ConfigError("workflow terminate needs --yes")
    if cmd == "mcp":
        raise AssertionError("mcp handled in main")

    client = await connect(profile, timeout_s=a.timeout,
                           identity=runtime.worker_identity(a.role) if cmd == "worker" else None)

    if cmd == "worker":
        runtime.setup_worker_logging(a.role, a.log_file)
        runtime.configure_notify(profile.notify)
        workers = runtime.build_worker(client, a.role, profile.workflow_queue, profile.exec_queue,
                                       a.max_concurrent)
        queues_used = {"control": [profile.workflow_queue], "exec": [profile.exec_queue]}.get(
            a.role, sorted({profile.workflow_queue, profile.exec_queue}))
        if sys.stderr is not None:
            print(f"legends-temporal worker role={a.role} address={profile.address} ns={profile.namespace} "
                  f"queues={queues_used}", file=sys.stderr, flush=True)
        await asyncio.gather(*(w.run() for w in workers))
        return {"ok": True}
    if cmd == "namespace":
        if sub == "list":
            return await namespaces.list_namespaces(client)
        return await namespaces.describe_namespace(client, a.name or profile.namespace)
    if cmd == "queue":
        names = a.task_queues or profile.watch_queues
        res = [await queues.describe_queue(client, profile.namespace, n) for n in names]
        return {"ok": all(r["healthy"] for r in res), "queues": res}
    if cmd == "schedule":
        if sub == "list":
            return await schedules.list_schedules(client, a.query)
        if sub == "describe":
            return await schedules.describe_schedule(client, a.schedule_id)
        if sub == "create":
            return await schedules.apply_definitions(client, profile, [schedules.flag_definition(a, profile)])
        if sub == "apply":
            defs = schedules.load_definitions(a.file, None, None) if a.file else _inventory(a, profile)
            return await schedules.apply_definitions(client, profile, defs, only=a.only)
        if sub == "trigger":
            return await schedules.trigger(client, a.schedule_id, a.overlap)
        if sub == "pause":
            return await schedules.pause(client, a.schedule_id, a.note)
        if sub == "unpause":
            return await schedules.unpause(client, a.schedule_id, a.note)
        if sub == "delete":
            return await schedules.delete(client, a.schedule_id)
        if sub == "backfill":
            return await schedules.backfill(client, a.schedule_id, _parse_dt(a.start), _parse_dt(a.end), a.overlap)
    if cmd == "timer":
        return await timers.start_timer(client, profile, job, delay_s, wait=a.wait, wait_timeout_s=a.wait_timeout)
    if cmd == "workflow":
        if sub == "list":
            q = workflows.build_query(a.status, a.wf_type, a.id_prefix, a.query)
            return await workflows.list_workflows(client, q, a.limit)
        if sub == "describe":
            return await workflows.describe_workflow(client, a.workflow_id, a.run_id)
        if sub == "history":
            return await workflows.history(client, a.workflow_id, a.run_id, a.diagnose, a.limit, a.tail_lines)
        if sub == "start":
            return await workflows.start(
                client, a.wf_type, a.id, a.queue or profile.workflow_queue, workflows.parse_input(a.input),
                timers.parse_delay(a.start_delay) if a.start_delay else 0,
                timers.parse_delay(a.execution_timeout) if a.execution_timeout else None, a.wait, a.wait_timeout)
        if sub == "signal":
            return await workflows.signal(client, a.workflow_id, a.name, workflows.parse_input(a.input), a.run_id)
        if sub == "query":
            return await workflows.query(client, a.workflow_id, a.name, workflows.parse_input(a.input), a.run_id)
        if sub == "cancel":
            return await workflows.cancel(client, a.workflow_id, a.run_id)
        if sub == "terminate":
            return await workflows.terminate(client, a.workflow_id, a.reason, a.run_id)
    if cmd == "job" and sub == "run":
        defs = {d["job_id"]: d for d in _inventory(a, profile)}
        if a.job_id not in defs:
            raise LegendsTemporalError("JOB_NOT_FOUND", f"{a.job_id} not in inventory ({sorted(defs)})",
                                       EXIT_NOT_FOUND)
        wire = runtime.to_wire({k: v for k, v in defs[a.job_id].items() if k in runtime.WIRE_FIELDS})
        return await workflows.run_job(client, profile.workflow_queue, wire, wait=not a.no_wait,
                                       timeout_s=a.run_timeout)
    raise ConfigError(f"unknown command {cmd} {sub or ''}")


def _exit_for(result: dict[str, Any]) -> int:
    if result.get("ok", True):
        return EXIT_OK
    if result.get("status") in {"FAILED", "WAIT_TIMEOUT", "TIMED_OUT", "TERMINATED", "CANCELED"}:
        return EXIT_WORKFLOW_FAILED
    return EXIT_ERROR


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    if getattr(a, "arg_extra", None):
        a.args = list(a.args or []) + list(a.arg_extra)
    try:
        profile = resolve_profile(a.profile, a.config, a.address, a.namespace)
        if a.cmd == "mcp":
            from .mcp_server import serve
            return serve(profile)
        result = asyncio.run(_dispatch(a, profile))
    except LegendsTemporalError as exc:
        result, code = exc.to_dict(), exc.exit_code
    except (ConfigError, ValueError, json.JSONDecodeError) as exc:
        result, code = {"ok": False, "error": "USAGE", "message": cluster_safe(exc)}, EXIT_USAGE
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        code = EXIT_ERROR
        try:
            from temporalio.service import RPCError, RPCStatusCode
            if isinstance(exc, RPCError) and exc.status == RPCStatusCode.NOT_FOUND:
                code = EXIT_NOT_FOUND
        except ImportError:
            pass
        result = {"ok": False, "error": "NOT_FOUND" if code == EXIT_NOT_FOUND else "TEMPORAL_ERROR",
                  "message": cluster_safe(exc)}
    else:
        code = _exit_for(result)
    if a.json or not result.get("ok", True):
        print(dumps(result))
    else:
        print(_human(a.cmd, getattr(a, "sub", None), result))
    return code


if __name__ == "__main__":
    sys.exit(main())
