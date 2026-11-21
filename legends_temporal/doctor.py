# SPDX-License-Identifier: MIT
"""`legends-temporal doctor [--live]`: offline readiness plus optional live cluster checks."""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .client import LegendsTemporalError, cluster_safe, connect, ssh_systemd, ui_status
from .config import Profile, config_path

ROOT = Path(__file__).resolve().parent.parent
REQUIRED_RESOURCES = (
    "docs/AGENT-RECIPE.md", "docs/CLI-CONTRACT.json", "docs/KNOWLEDGE-PACK.md",
    "knowledge/temporal/pack.json", "skills/cto-legends/SKILL.md",
)
KNOWLEDGE_NOTES = (
    "01-temporal-mental-model-and-determinism.md", "02-schedules-and-durable-timers.md",
    "03-zero-rewrite-scriptjob-and-agentjob-pattern.md", "04-timeouts-retries-and-idempotency.md",
    "05-self-hosted-postgres-and-tailnet-hardening.md", "06-visibility-queries-and-failure-diagnosis.md",
)
MIN_SDK = (1, 8)


def _check(name: str, status: str, detail: Any) -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail}


def resource_paths(root: Path = ROOT) -> dict[str, str]:
    out = {rel: str(root / rel) for rel in REQUIRED_RESOURCES}
    for note in KNOWLEDGE_NOTES:
        out[f"knowledge/temporal/{note}"] = str(root / "knowledge" / "temporal" / note)
    return out


def offline_checks(profile: Profile, config: str | None = None, root: Path = ROOT) -> list[dict[str, Any]]:
    checks = []
    py_ok = sys.version_info >= (3, 11)
    checks.append(_check("python", "PASS" if py_ok else "FAIL", sys.version.split()[0]))
    try:
        import importlib.metadata as md
        ver = md.version("temporalio")
        parts = tuple(int(x) for x in ver.split(".")[:2])
        checks.append(_check("temporalio_sdk", "PASS" if parts >= MIN_SDK else "FAIL", ver))
    except Exception:  # noqa: BLE001
        checks.append(_check("temporalio_sdk", "FAIL", "not installed: python -m pip install temporalio"))
    cli = shutil.which("temporal")
    checks.append(_check("temporal_cli", "PASS" if cli else "WARN",
                         cli or "optional `temporal` CLI not on PATH (not required by this module)"))
    cpath = config_path(config)
    checks.append(_check("config", "PASS", {"path": str(cpath), "exists": cpath.exists(),
                                            "profile": profile.name, "source": profile.source,
                                            "address": profile.address, "namespace": profile.namespace,
                                            "tls": bool(profile.tls or profile.tls_cert),
                                            "api_key_present": bool(profile.api_key())}))
    missing = [rel for rel, p in resource_paths(root).items() if not Path(p).exists()]
    pack_ok = True
    pack = root / "knowledge" / "temporal" / "pack.json"
    if pack.exists():
        try:
            pdata = json.loads(pack.read_text(encoding="utf-8"))
            pack_ok = pdata.get("schema") == "legends.knowledge-pack/v1"
        except json.JSONDecodeError:
            pack_ok = False
    checks.append(_check("recipe_and_knowledge_pack", "PASS" if not missing and pack_ok else "WARN",
                         {"root": str(root), "missing": missing, "pack_schema_ok": pack_ok}))
    return checks


async def live_checks(profile: Profile, timeout_s: float = 10.0) -> list[dict[str, Any]]:
    from .namespaces import describe_namespace
    from .queues import describe_queue

    checks = []
    t = time.monotonic()
    try:
        client = await connect(profile, timeout_s=timeout_s)
    except LegendsTemporalError as exc:
        checks.append(_check("grpc_connect", "FAIL", exc.message))
        return checks
    checks.append(_check("grpc_connect", "PASS", {"address": profile.address,
                                                  "latency_ms": round((time.monotonic() - t) * 1000)}))
    try:
        ns = await describe_namespace(client, profile.namespace)
        checks.append(_check("namespace", "PASS" if ns["state"] == "REGISTERED" else "FAIL",
                             {"name": ns["name"], "state": ns["state"], "retention_days": ns["retention_days"],
                              "cluster": ns.get("cluster"), "server_version": ns.get("server_version")}))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("namespace", "FAIL", cluster_safe(exc)))
    for q in profile.watch_queues:
        try:
            qd = await describe_queue(client, profile.namespace, q)
            pollers = [{"kind": k.split("_")[0], "identity": p["identity"], "age_s": p["age_s"]}
                       for k in ("workflow_pollers", "activity_pollers") for p in qd[k]]
            checks.append(_check(f"queue:{q}", "PASS" if qd["healthy"] else "FAIL",
                                 {"live_pollers": qd["live_pollers"], "pollers": pollers}))
        except Exception as exc:  # noqa: BLE001
            checks.append(_check(f"queue:{q}", "FAIL", cluster_safe(exc)))
    if profile.ui_url:
        ui = ui_status(profile.ui_url, resolve_ip=profile.ui_resolve)
        checks.append(_check("ui_http", "PASS" if ui.get("ok") else "WARN", ui))
    if profile.ssh.host:
        ss = ssh_systemd(profile.ssh.host, profile.ssh.units, profile.ssh.user_units, profile.ssh.ssh_bin)
        checks.append(_check("ssh_systemd", "PASS" if ss.get("ok") else "WARN", ss))
    return checks


async def run_doctor(profile: Profile, live: bool = False, config: str | None = None,
                     timeout_s: float = 10.0) -> dict[str, Any]:
    checks = offline_checks(profile, config)
    if live:
        checks += await live_checks(profile, timeout_s)
    failed = [c["name"] for c in checks if c["status"] == "FAIL"]
    warned = [c["name"] for c in checks if c["status"] == "WARN"]
    return {"ok": not failed, "tool": "legends-temporal", "version": __version__, "live": live,
            "profile": profile.name, "failed": failed, "warnings": warned, "checks": checks}
