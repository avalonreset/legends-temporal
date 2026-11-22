# SPDX-License-Identifier: MIT
"""Zero-Rewrite ScriptJob / AgentJob runtime: wire types, job builders, activities,
workflow and worker.

Wire contract (stable, shared with existing `ScriptJob` workers):
  workflow type  "ScriptJob"
  activities     "run_script" (exec queue), "notify_buzz" (exec queue), "log_failure" (workflow queue)
  payload        ScriptJob with exactly WIRE_FIELDS (no extra keys: older workers reject them)

A script runs unchanged as [interpreter, script, *args] on the machine that owns
its data. Scripts are not assumed idempotent: run_script never retries.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

from .config import NotifyConfig

log = logging.getLogger("legends_temporal")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: never flash a console
TAIL_CHARS = 1500
NOTICE_TAIL_CHARS = 600

WIRE_FIELDS = ("job_id", "title", "queue", "interpreter", "script", "args", "cwd",
               "timeout_s", "wait_for_worker_s", "notify_channel", "cron")


@dataclass
class ScriptJob:
    """Run one existing script unchanged on the machine that owns its data."""
    job_id: str
    title: str
    queue: str
    interpreter: str
    script: str
    args: list[str] = field(default_factory=list)
    cwd: str = ""
    timeout_s: int = 900
    wait_for_worker_s: int = 6 * 3600
    notify_channel: str = "ops"
    cron: str = ""


@dataclass
class ScriptResult:
    exit_code: int
    duration_s: float
    tail: str


@dataclass
class Notice:
    channel: str
    text: str


# ---------------- job builders (pure) ----------------

def to_wire(job: Any) -> dict[str, Any]:
    """Any ScriptJob-like object or dict -> exact wire dict (extra keys dropped)."""
    src = job if isinstance(job, dict) else {k: getattr(job, k) for k in WIRE_FIELDS if hasattr(job, k)}
    missing = [k for k in ("job_id", "title", "queue", "interpreter", "script") if not src.get(k)]
    if missing:
        raise ValueError(f"job is missing required fields: {missing}")
    defaults = asdict(ScriptJob(job_id="", title="", queue="", interpreter="", script=""))
    wire = {k: src.get(k, defaults[k]) for k in WIRE_FIELDS}
    wire["args"] = [str(a) for a in (wire["args"] or [])]
    wire["timeout_s"] = int(wire["timeout_s"])
    wire["wait_for_worker_s"] = int(wire["wait_for_worker_s"])
    wire["cwd"] = wire["cwd"] or ""
    wire["cron"] = wire["cron"] or ""
    return wire


def remaining_actions_of(job: Any) -> int:
    if isinstance(job, dict):
        return int(job.get("remaining_actions") or 0)
    return int(getattr(job, "_remaining_actions", 0) or getattr(job, "remaining_actions", 0) or 0)


def script_job(job_id: str, title: str, queue: str, interpreter: str, script: str,
               args: list[str] | None = None, cwd: str = "", timeout_s: int = 900,
               wait_for_worker_s: int = 6 * 3600, notify_channel: str = "ops", cron: str = "") -> ScriptJob:
    return ScriptJob(job_id=job_id, title=title, queue=queue, interpreter=interpreter, script=script,
                     args=list(args or []), cwd=cwd, timeout_s=timeout_s,
                     wait_for_worker_s=wait_for_worker_s, notify_channel=notify_channel, cron=cron)


def code_job(job_id: str, title: str, queue: str, interpreter: str, code: str,
             args: list[str] | None = None, **kw: Any) -> ScriptJob:
    return script_job(job_id, title, queue, interpreter, "-c", [code, *(args or [])], **kw)


def module_job(job_id: str, title: str, queue: str, interpreter: str, module_name: str,
               args: list[str] | None = None, **kw: Any) -> ScriptJob:
    """Run an installed cto-legends module inside its isolated release environment via
    `python -m cto_legends run <module> -- <args>` while keeping the 11-field ScriptJob wire contract."""
    import re
    mod = (module_name or "").strip().lower()
    if not re.fullmatch(r"legends-[a-z0-9-]+", mod):
        raise ValueError(f"invalid cto-legends module slug '{module_name}' (expected legends-<name>)")
    mod_args = ["cto_legends", "run", mod]
    if args:
        mod_args += ["--", *[str(a) for a in args]]
    return script_job(job_id, title, queue, interpreter, "-m", mod_args, **kw)


def _ensure_stipend_depth_header(message: str, depth: int = 1) -> str:
    if not message or message.lstrip().startswith("[stipend depth"):
        return message
    return f"[stipend depth {depth}]\n{message}"


def agent_job(job_id: str, title: str, queue: str, interpreter: str, agent_ctl: str,
              session_id: str | None = None, message: str = "", model: str | None = None, cwd: str = "",
              timeout_s: int = 120, notify_channel: str = "ops", cron: str = "",
              wait_for_worker_s: int = 6 * 3600, *, spawn: bool = False,
              agent: str | None = None, effort: str | None = None, task_type: str = "build",
              project: str | None = None, workspace: str = "",
              excluded_agents: dict[str, str] | None = None,
              sessions: dict[str, str] | None = None) -> ScriptJob:
    """Dispatch an agent session turn (`send`) or spawn a fresh session (`spawn`)
    via `agent_ctl.py` (no UI approval modal, no minimum interval)."""
    if not agent_ctl:
        raise ValueError("agent jobs need agent_ctl (profile.agent_ctl or --agent-ctl)")
    if not message:
        raise ValueError("agent jobs need a message")
    if spawn or (agent and not session_id):
        agent_arg = agent.strip() if agent else "auto"
        excl = {k.lower(): v for k, v in (excluded_agents or {}).items()}
        if agent_arg.lower() in excl:
            raise ValueError(f"Agent {agent_arg!r} is excluded: {excl[agent_arg.lower()]}")
        model_arg = model.strip() if model else "auto"
        ws = workspace or cwd
        msg = _ensure_stipend_depth_header(message, 1)
        args = ["spawn", "--agent", agent_arg]
        if ws:
            args += ["--workspace", ws]
        args += [
            "--title",
            title,
            "--model",
            model_arg,
            "--type",
            task_type or "build",
            "--stipend-depth",
            "1",
            "--message",
            msg,
        ]
        if effort:
            args += ["--effort", effort]
        if project:
            args += ["--project", project]
        return script_job(job_id, title, queue, interpreter, agent_ctl, args, cwd=ws, timeout_s=timeout_s,
                          wait_for_worker_s=wait_for_worker_s, notify_channel=notify_channel, cron=cron)
    if not session_id:
        raise ValueError("agent jobs need either session_id or spawn=True / --agent")
    resolved_session = (sessions or {}).get(session_id, session_id)
    args = ["send", resolved_session, "--message", message]
    if model:
        args += ["--model", model]
    return script_job(job_id, title, queue, interpreter, agent_ctl, args, cwd=cwd, timeout_s=timeout_s,
                      wait_for_worker_s=wait_for_worker_s, notify_channel=notify_channel, cron=cron)


def command_job(job_id: str, title: str, queue: str, interpreter: str, argv: list[str], **kw: Any) -> ScriptJob:
    """Run an arbitrary argv (shell=False) via the worker's Python, propagating its exit code."""
    if not argv:
        raise ValueError("command mode needs at least one argv element")
    code = ("import json,subprocess,sys;"
            f"sys.exit(subprocess.call(json.loads({json.dumps(json.dumps(argv))})))")
    return code_job(job_id, title, queue, interpreter, code, **kw)


def webhook_job(job_id: str, title: str, queue: str, interpreter: str, url_env: str,
                body: dict[str, Any], **kw: Any) -> ScriptJob:
    """POST a JSON body to a URL read from an env var on the worker (the URL is never stored)."""
    payload = json.dumps(body)
    code = ("import os,sys,urllib.request as u;"
            f"url=os.environ.get({url_env!r}) or sys.exit({('webhook env var ' + url_env + ' is not set on this worker')!r});"
            f"r=u.urlopen(u.Request(url,data={payload!r}.encode(),"
            "headers={'Content-Type':'application/json'},method='POST'),timeout=30);"
            "print('webhook status',r.status)")
    return code_job(job_id, title, queue, interpreter, code, **kw)


def schedule_execution_timeout(job: dict[str, Any]) -> timedelta:
    return timedelta(seconds=int(job["wait_for_worker_s"]) + int(job["timeout_s"]) + 3 * 3600)


def timer_execution_timeout(job: dict[str, Any], delay_s: int) -> timedelta:
    return timedelta(seconds=delay_s + int(job["wait_for_worker_s"]) + int(job["timeout_s"]) + 3600)


def failure_notice_text(title: str, queue: str, failure: str, workflow_id: str, tail: str) -> str:
    tail = (tail or "")[-NOTICE_TAIL_CHARS:]
    return (f"⚠️ {title} failed on {queue} ({failure}).\n"
            f"Run: {workflow_id}\n"
            + (f"Last output:\n{tail}" if tail else "No output (the script did not run)."))


def subprocess_flags() -> int:
    return NO_WINDOW


# ---------------- notifier (configured per worker process) ----------------

_NOTIFY = NotifyConfig()


def configure_notify(cfg: NotifyConfig) -> None:
    global _NOTIFY
    _NOTIFY = cfg


def send_notice(notice: Notice, cfg: NotifyConfig | None = None) -> str:
    cfg = cfg or _NOTIFY
    if cfg.kind == "none":
        return "suppressed"
    if cfg.kind == "stdout":
        print(f"LEGENDS_TEMPORAL_NOTICE [{notice.channel}] " + notice.text.replace("\n", " | "), flush=True)
        return "stdout"
    if cfg.kind == "webhook":
        import urllib.request
        url = os.environ.get(cfg.webhook_url_env, "")
        if not url:
            raise RuntimeError(f"webhook env var {cfg.webhook_url_env} is not set")
        data = json.dumps({"channel": notice.channel, "text": notice.text}).encode()
        req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - operator-configured
            return f"webhook:{resp.status}"
    if cfg.kind == "buzz":
        channel = cfg.channels.get(notice.channel, notice.channel)
        env = dict(os.environ)
        if cfg.buzz_identity_file:
            with open(cfg.buzz_identity_file, encoding="utf-8") as fh:
                env["BUZZ_PRIVATE_KEY"] = json.load(fh)["private_key"]
        cmd = [cfg.buzz_exe]
        if cfg.buzz_relay:
            cmd += ["--relay", cfg.buzz_relay]
        cmd += ["messages", "send", "--channel", channel, "--content", "-"]
        proc = subprocess.run(cmd, input=notice.text, capture_output=True, text=True, encoding="utf-8",
                              timeout=60, env=env, creationflags=NO_WINDOW)
        if proc.returncode != 0:
            raise RuntimeError(f"buzz send failed ({proc.returncode}): {(proc.stderr or proc.stdout)[-300:]}")
        return f"buzz:{notice.channel}"
    raise RuntimeError(f"unknown notify kind {cfg.kind}")


def execute_script(job: ScriptJob) -> ScriptResult:
    cmd = [job.interpreter, job.script, *job.args]
    started = time.monotonic()
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        proc = subprocess.run(cmd, cwd=job.cwd or None, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=job.timeout_s, env=env, creationflags=NO_WINDOW)
        code, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        code, out = 124, f"{exc.stdout or ''}{exc.stderr or ''}\nTIMEOUT after {job.timeout_s}s"
    except FileNotFoundError as exc:
        code, out = 127, f"interpreter or script not found: {exc}"
    return ScriptResult(exit_code=code, duration_s=round(time.monotonic() - started, 1), tail=out[-TAIL_CHARS:])


# ---------------- Temporal definitions ----------------

try:
    from temporalio import activity, workflow
    from temporalio.common import RetryPolicy
    from temporalio.exceptions import ActivityError, ApplicationError
    HAVE_SDK = True
except ImportError:  # pragma: no cover
    HAVE_SDK = False

if HAVE_SDK:
    @activity.defn(name="run_script")
    def run_script(job: ScriptJob) -> ScriptResult:
        log.info("run_script start job=%s", job.job_id)
        res = execute_script(job)
        log.info("run_script end job=%s exit=%s duration=%ss", job.job_id, res.exit_code, res.duration_s)
        return res

    @activity.defn(name="notify_buzz")
    def notify_buzz(notice: Notice) -> str:
        return send_notice(notice)

    @activity.defn(name="log_failure")
    def log_failure(text: str) -> None:
        print("LEGENDS_TEMPORAL_FAILURE " + text.replace("\n", " | "), flush=True)

    @workflow.defn(name="ScriptJob")
    class ScriptJobWorkflow:
        @workflow.run
        async def run(self, job: ScriptJob) -> dict:
            failure = ""
            result: ScriptResult | None = None
            try:
                result = await workflow.execute_activity(
                    "run_script", job, result_type=ScriptResult, task_queue=job.queue,
                    schedule_to_start_timeout=timedelta(seconds=job.wait_for_worker_s),
                    start_to_close_timeout=timedelta(seconds=job.timeout_s + 60),
                    retry_policy=RetryPolicy(maximum_attempts=1))
                if result.exit_code != 0:
                    failure = f"exit code {result.exit_code}"
            except ActivityError as err:
                cause = err.cause
                failure = f"{type(cause).__name__ if cause else 'ActivityError'}: {cause or err}"
            if not failure:
                return {"job": job.job_id, "ok": True, "exit_code": 0,
                        "duration_s": result.duration_s if result else None}
            text = failure_notice_text(job.title, job.queue, failure, workflow.info().workflow_id,
                                       result.tail if result else "")
            await workflow.execute_activity(
                "log_failure", text, task_queue=workflow.info().task_queue,
                start_to_close_timeout=timedelta(minutes=1), retry_policy=RetryPolicy(maximum_attempts=3))
            posted = True
            try:
                await workflow.execute_activity(
                    "notify_buzz", Notice(channel=job.notify_channel, text=text), task_queue=job.queue,
                    schedule_to_close_timeout=timedelta(hours=2),
                    retry_policy=RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=30)))
            except ActivityError:
                posted = False
            raise ApplicationError(f"{job.title}: {failure}" + ("" if posted else " (notice failed; see worker log)"),
                                   non_retryable=True)


def build_worker(client: Any, role: str, workflow_queue: str, exec_queue: str, max_concurrent: int = 2):
    """role: control (workflow + log_failure), exec (run_script + notify), all (single queue)."""
    import concurrent.futures

    from temporalio.worker import Worker
    from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(4, max_concurrent + 2))
    runner = SandboxedWorkflowRunner(restrictions=SandboxRestrictions.default.with_passthrough_modules(
        "legends_temporal"))
    if role == "control":
        return [Worker(client, task_queue=workflow_queue, workflows=[ScriptJobWorkflow], activities=[log_failure],
                       activity_executor=pool, workflow_runner=runner)]
    if role == "exec":
        return [Worker(client, task_queue=exec_queue, activities=[run_script, notify_buzz], activity_executor=pool,
                       max_concurrent_activities=max_concurrent)]
    if role == "all":
        queues = {workflow_queue, exec_queue}
        workers = []
        for q in sorted(queues):
            acts = []
            if q == workflow_queue:
                acts.append(log_failure)
            if q == exec_queue:
                acts += [run_script, notify_buzz]
            workers.append(Worker(client, task_queue=q,
                                  workflows=[ScriptJobWorkflow] if q == workflow_queue else [],
                                  activities=acts, activity_executor=pool, workflow_runner=runner,
                                  max_concurrent_activities=max_concurrent + 1))
        return workers
    raise ValueError("role must be control, exec or all")


def worker_identity(role: str) -> str:
    return f"legends-temporal-{role}@{socket.gethostname()}"


def setup_worker_logging(role: str, log_file: str | None = None) -> None:
    handlers: list[logging.Handler] = []
    if log_file:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    if sys.stdout is not None:  # pythonw has no stdout
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, handlers=handlers or [logging.NullHandler()],
                        format=f"%(asctime)s {role} %(levelname)s %(name)s %(message)s")
