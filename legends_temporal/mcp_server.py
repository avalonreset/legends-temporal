# SPDX-License-Identifier: MIT
"""`legends-temporal mcp`: a small stdio JSON-RPC 2.0 MCP server.

Newline-delimited JSON over stdin/stdout (MCP stdio transport). Tools are
read-only except `temporal_timer_schedule`, which only previews unless the
caller passes `confirm: true`. Logs go to stderr, never stdout.
"""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any, Callable

from . import __version__
from .client import LegendsTemporalError, cluster_safe, connect, jsonable
from .config import Profile

PROTOCOL_VERSION = "2025-06-18"

TOOLS: list[dict[str, Any]] = [
    {"name": "temporal_doctor", "description": "Readiness checks for the configured Temporal profile.",
     "inputSchema": {"type": "object", "properties": {"live": {"type": "boolean", "default": True}},
                     "additionalProperties": False}},
    {"name": "temporal_namespace_list", "description": "List namespaces with state and retention.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "temporal_queue_describe", "description": "Show live pollers on a task queue.",
     "inputSchema": {"type": "object", "properties": {"task_queue": {"type": "string"}},
                     "required": ["task_queue"], "additionalProperties": False}},
    {"name": "temporal_schedule_list", "description": "List schedules with spec, paused state and next run.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "additionalProperties": False}},
    {"name": "temporal_schedule_describe", "description": "Describe one schedule (spec, policy, state, recent runs).",
     "inputSchema": {"type": "object", "properties": {"schedule_id": {"type": "string"}},
                     "required": ["schedule_id"], "additionalProperties": False}},
    {"name": "temporal_workflow_list", "description": "List workflow executions by status/type/id prefix/query.",
     "inputSchema": {"type": "object", "properties": {
         "status": {"type": "string"}, "type": {"type": "string"}, "id_prefix": {"type": "string"},
         "query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10}},
         "additionalProperties": False}},
    {"name": "temporal_workflow_diagnose", "description": "Compact failure diagnosis of one workflow run: failed "
     "activity, exit code, output tail, attempts, timeline.",
     "inputSchema": {"type": "object", "properties": {
         "workflow_id": {"type": "string"}, "run_id": {"type": "string"},
         "tail_lines": {"type": "integer", "minimum": 0, "maximum": 200, "default": 20}},
         "required": ["workflow_id"], "additionalProperties": False}},
    {"name": "temporal_timer_schedule", "description": "Schedule a one-shot durable delayed timer (script, inline "
     "code, cto-legends module, or agent session wake). Returns a preview unless confirm is true.",
     "inputSchema": {"type": "object", "properties": {
         "delay": {"type": "string", "description": "e.g. 30s, 15m, 2h, 1h30m"},
         "code": {"type": "string"}, "script": {"type": "string"}, "module": {"type": "string"},
         "args": {"type": "array", "items": {"type": "string"}},
         "session": {"type": "string"}, "message": {"type": "string"}, "model": {"type": "string"},
         "title": {"type": "string"}, "notify_channel": {"type": "string"},
         "confirm": {"type": "boolean", "default": False}},
         "required": ["delay"], "additionalProperties": False}},
    {"name": "temporal_schedule_audit", "description": "Audit schedule health, live task-queue pollers, secret hygiene "
     "and optional declarative inventory drift.",
     "inputSchema": {"type": "object", "properties": {
         "file": {"type": "string", "description": "Optional JSON/YAML definitions file path"},
         "offline": {"type": "boolean", "default": False}},
         "additionalProperties": False}},
    {"name": "temporal_bootstrap_plan", "description": "Preview a SQLite-persisted local Temporal dev server command, "
     "hardened Docker Compose stack, or systemd unit blueprint (read-only preview).",
     "inputSchema": {"type": "object", "properties": {
         "target": {"type": "string", "enum": ["local", "compose", "systemd"], "default": "local"},
         "bind_ip": {"type": "string", "default": "127.0.0.1"},
         "role": {"type": "string", "enum": ["control", "exec", "all"], "default": "control"}},
         "additionalProperties": False}},
]
TOOL_NAMES = {t["name"] for t in TOOLS}


async def _with_client(profile: Profile, fn: Callable[[Any], Any]) -> Any:
    client = await connect(profile)
    return await fn(client)


async def call_tool(profile: Profile, name: str, args: dict[str, Any]) -> dict[str, Any]:
    from . import doctor, namespaces, queues, schedules, timers, workflows

    if name == "temporal_doctor":
        return await doctor.run_doctor(profile, live=bool(args.get("live", True)))
    if name == "temporal_namespace_list":
        return await _with_client(profile, namespaces.list_namespaces)
    if name == "temporal_queue_describe":
        return await _with_client(profile, lambda c: queues.describe_queue(c, profile.namespace, args["task_queue"]))
    if name == "temporal_schedule_list":
        return await _with_client(profile, lambda c: schedules.list_schedules(c, args.get("query")))
    if name == "temporal_schedule_describe":
        return await _with_client(profile, lambda c: schedules.describe_schedule(c, args["schedule_id"]))
    if name == "temporal_workflow_list":
        q = workflows.build_query(args.get("status"), args.get("type"), args.get("id_prefix"), args.get("query"))
        return await _with_client(profile, lambda c: workflows.list_workflows(c, q, int(args.get("limit", 10))))
    if name == "temporal_workflow_diagnose":
        return await _with_client(profile, lambda c: workflows.history(
            c, args["workflow_id"], args.get("run_id"), diagnose=True, tail_lines=int(args.get("tail_lines", 20))))
    if name == "temporal_timer_schedule":
        delay_s = timers.parse_delay(args["delay"])
        job = timers.build_timer_job(profile, args["delay"], script=args.get("script"), code=args.get("code"),
                                     module=args.get("module"),
                                     session=args.get("session"), message=args.get("message"),
                                     model=args.get("model"), args=args.get("args"), title=args.get("title", ""),
                                     notify_channel=args.get("notify_channel"))
        from .runtime import to_wire
        if not args.get("confirm"):
            return {"ok": True, "preview": True, "fires_in_s": delay_s, "job": to_wire(job),
                    "workflow_queue": profile.workflow_queue, "note": "call again with confirm=true to schedule"}
        return await _with_client(profile, lambda c: timers.start_timer(c, profile, job, delay_s))
    if name == "temporal_schedule_audit":
        from . import audit
        defs = schedules.load_definitions(args["file"]) if args.get("file") else None
        if args.get("offline"):
            if defs is None:
                raise ValueError("offline audit requires 'file'")
            res = audit.audit_definitions(defs, profile)
            res.pop("_audited_by_id", None)
            return res
        return await _with_client(profile, lambda c: audit.audit_cluster(c, profile, defs=defs))
    if name == "temporal_bootstrap_plan":
        from . import bootstrap
        target = args.get("target", "local")
        if target == "local":
            return bootstrap.plan_local(profile, start=False, dry_run=True)
        if target == "compose":
            return bootstrap.plan_compose(profile, bind_ip=args.get("bind_ip", "127.0.0.1"), write=False, dry_run=True)
        if target == "systemd":
            return bootstrap.plan_systemd(profile, role=args.get("role", "control"), write=False, dry_run=True)
        raise ValueError(f"unknown bootstrap target: {target}")
    raise KeyError(name)


def handle_message(msg: dict[str, Any], profile: Profile) -> dict[str, Any] | None:
    """Handle one JSON-RPC message; returns a response dict or None for notifications."""
    mid = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params") or {}

    def ok(result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    if mid is None:  # notification (e.g. notifications/initialized)
        return None
    if method == "initialize":
        return ok({"protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "legends-temporal", "version": __version__}})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        if name not in TOOL_NAMES:
            return err(-32602, f"unknown tool: {name}")
        try:
            result = asyncio.run(call_tool(profile, name, params.get("arguments") or {}))
            is_error = isinstance(result, dict) and result.get("ok") is False
        except LegendsTemporalError as exc:
            result, is_error = exc.to_dict(), True
        except (KeyError, ValueError) as exc:
            result, is_error = {"ok": False, "error": "BAD_ARGUMENTS", "message": cluster_safe(exc)}, True
        except Exception as exc:  # noqa: BLE001
            result, is_error = {"ok": False, "error": "TEMPORAL_ERROR", "message": cluster_safe(exc)}, True
        text = json.dumps(jsonable(result), ensure_ascii=False)
        return ok({"content": [{"type": "text", "text": text}], "isError": is_error})
    return err(-32601, f"method not found: {method}")


def serve(profile: Profile, stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    print(f"legends-temporal mcp {__version__} profile={profile.name} address={profile.address}", file=sys.stderr)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            resp: dict[str, Any] | None = {"jsonrpc": "2.0", "id": None,
                                           "error": {"code": -32700, "message": "parse error"}}
        else:
            resp = handle_message(msg, profile) if isinstance(msg, dict) else {
                "jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0
