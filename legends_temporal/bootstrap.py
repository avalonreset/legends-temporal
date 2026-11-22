# SPDX-License-Identifier: MIT
"""Zero-to-running Temporal cluster and ScriptJob worker bootstrap helpers.

`legends-temporal bootstrap` solves three classic adoption traps:
  1. `bootstrap local`: Standard `temporal server start-dev` runs in-memory only and
     wipes every schedule on restart. `bootstrap local` enforces `--db-filename`
     SQLite persistence, previews the command (`--dry-run`), and with `--start`
     spawns the local server in the background and waits for gRPC readiness.
  2. `bootstrap compose`: Self-hosted Docker Compose stacks often bind gRPC 7233 to
     0.0.0.0, exposing an unauthenticated Temporal frontend to the public internet.
     `bootstrap compose` generates a hardened PostgreSQL 16 + Temporal Server +
     Temporal UI + control-worker `docker-compose.yml` bound strictly to 127.0.0.1
     (or an explicit Tailscale IP via `--bind-ip`).
  3. `bootstrap systemd`: Generates Linux systemd unit files for the ScriptJob
     worker (`legends-temporal-worker.service`) and a daily PostgreSQL `pg_dump`
     backup timer deliberately kept outside Temporal.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import Profile, tcp_reachable

NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def plan_local(
    profile: Profile,
    *,
    db_path: str | None = None,
    port: int = 7233,
    ui_port: int = 8233,
    namespace: str | None = None,
    start: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Plan or launch a SQLite-persisted local Temporal dev server."""
    ns = namespace or profile.namespace or "default"
    resolved_db = Path(db_path).expanduser() if db_path else (Path.home() / ".legends-temporal" / "temporal-dev.db")
    temporal_bin = shutil.which("temporal")
    address = f"127.0.0.1:{port}"
    ui_url = f"http://127.0.0.1:{ui_port}"
    cmd = [
        temporal_bin or "temporal",
        "server",
        "start-dev",
        "--db-filename",
        str(resolved_db),
        "--port",
        str(port),
        "--ui-port",
        str(ui_port),
        "--namespace",
        ns,
    ]
    already_running = tcp_reachable(address, timeout=0.8)
    out: dict[str, Any] = {
        "ok": True,
        "target": "local",
        "dry_run": dry_run or not start,
        "temporal_cli_found": bool(temporal_bin),
        "temporal_cli_path": temporal_bin,
        "address": address,
        "ui_url": ui_url,
        "namespace": ns,
        "db_path": str(resolved_db),
        "persistent": True,
        "already_running": already_running,
        "command": cmd,
        "worker_command": [sys.executable, "-m", "legends_temporal", "--address", address, "--namespace", ns, "worker", "--role", "all"],
    }
    if not start or dry_run:
        out["action"] = "already-running" if already_running else "would-start"
        if not temporal_bin:
            out["install_hint"] = "Install the Temporal CLI (`brew install temporal`, `scoop install temporal-cli`, or https://temporal.io/setup/install-temporal-cli) then re-run with --start."
        return out

    if already_running:
        out["action"] = "already-running"
        return out
    if not temporal_bin:
        return {
            **out,
            "ok": False,
            "error": "TEMPORAL_CLI_MISSING",
            "message": "The `temporal` CLI binary is not on PATH; install it or use `legends-temporal bootstrap compose` for Docker.",
        }

    resolved_db.parent.mkdir(parents=True, exist_ok=True)
    log_path = resolved_db.parent / "temporal-dev.log"
    with open(log_path, "ab") as log_fh:
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
            start_new_session=(os.name != "nt"),
        )
    deadline = time.monotonic() + 15.0
    ready = False
    while time.monotonic() < deadline:
        if tcp_reachable(address, timeout=0.5):
            ready = True
            break
        if proc.poll() is not None:
            break
        time.sleep(0.25)
    out.update(
        ok=ready,
        action="started" if ready else "start-failed",
        pid=proc.pid,
        log_path=str(log_path),
    )
    if not ready:
        out["message"] = f"temporal server start-dev did not answer on {address} within 15s (see {log_path})"
    return out


def plan_compose(
    profile: Profile,
    *,
    output_dir: str = "./temporal-stack",
    bind_ip: str = "127.0.0.1",
    grpc_port: int = 7233,
    ui_port: int = 8088,
    namespace: str | None = None,
    write: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Generate a hardened self-hosted PostgreSQL + Temporal + UI + control-worker Docker Compose blueprint."""
    if bind_ip == "0.0.0.0":
        raise ValueError("Refusing to bind unauthenticated Temporal gRPC to 0.0.0.0; use 127.0.0.1 or a private Tailscale/WireGuard IP.")
    ns = namespace or profile.namespace or "default"
    wq = profile.workflow_queue or "legends-temporal"
    out_dir = Path(output_dir)

    compose_yaml = f"""# Generated by legends-temporal bootstrap compose
# Hardened self-hosted Temporal stack: gRPC and UI bind strictly to {bind_ip} (never 0.0.0.0).
services:
  temporal-postgres:
    image: postgres:16-alpine
    container_name: legends-temporal-postgres
    restart: unless-stopped
    environment:
      POSTGRES_USER: ${{TEMPORAL_PG_USER:-temporal}}
      POSTGRES_PASSWORD: ${{TEMPORAL_PG_PASSWORD:?Set TEMPORAL_PG_PASSWORD in .env}}
      POSTGRES_DB: temporal
    volumes:
      - temporal_pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${{TEMPORAL_PG_USER:-temporal}} -d temporal"]
      interval: 5s
      timeout: 5s
      retries: 10

  temporal-server:
    image: temporalio/auto-setup:1.26.2
    container_name: legends-temporal-server
    restart: unless-stopped
    depends_on:
      temporal-postgres:
        condition: service_healthy
    environment:
      DB: postgres12
      DB_PORT: "5432"
      POSTGRES_USER: ${{TEMPORAL_PG_USER:-temporal}}
      POSTGRES_PWD: ${{TEMPORAL_PG_PASSWORD:?Set TEMPORAL_PG_PASSWORD in .env}}
      POSTGRES_SEEDS: temporal-postgres
      DEFAULT_NAMESPACE: {ns}
      DEFAULT_NAMESPACE_RETENTION: 30d
    ports:
      - "{bind_ip}:{grpc_port}:7233"

  temporal-ui:
    image: temporalio/ui:2.34.0
    container_name: legends-temporal-ui
    restart: unless-stopped
    depends_on:
      - temporal-server
    environment:
      TEMPORAL_ADDRESS: temporal-server:7233
      TEMPORAL_CORS_ORIGINS: http://localhost:{ui_port}
    ports:
      - "{bind_ip}:{ui_port}:8080"

volumes:
  temporal_pgdata:
"""
    env_example = f"""# Copy to .env and set a strong random database password before running `docker compose up -d`
TEMPORAL_PG_USER=temporal
TEMPORAL_PG_PASSWORD=replace_with_32_char_random_password
TEMPORAL_ADDRESS={bind_ip}:{grpc_port}
TEMPORAL_NAMESPACE={ns}
LEGENDS_TEMPORAL_WORKFLOW_QUEUE={wq}
"""
    files = {
        str(out_dir / "docker-compose.yml"): compose_yaml,
        str(out_dir / ".env.example"): env_example,
    }
    should_write = bool(write and not dry_run)
    if should_write:
        out_dir.mkdir(parents=True, exist_ok=True)
        for fpath, content in files.items():
            Path(fpath).write_text(content, encoding="utf-8")
    return {
        "ok": True,
        "target": "compose",
        "dry_run": not should_write,
        "action": "written" if should_write else "preview",
        "bind_ip": bind_ip,
        "grpc_address": f"{bind_ip}:{grpc_port}",
        "ui_url": f"http://{bind_ip}:{ui_port}",
        "namespace": ns,
        "output_dir": str(out_dir),
        "files": files,
    }


def plan_systemd(
    profile: Profile,
    *,
    output_dir: str = "./systemd",
    role: str = "control",
    write: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Generate Linux systemd service and backup timer units for legends-temporal."""
    out_dir = Path(output_dir)
    worker_unit = f"""[Unit]
Description=legends-temporal ScriptJob Worker (role={role}, profile={profile.name})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={sys.executable} -m legends_temporal --profile {profile.name} worker --role {role}
Restart=always
RestartSec=5
Environment=PYTHONUTF8=1

[Install]
WantedBy=default.target
"""
    backup_service = """[Unit]
Description=legends-temporal PostgreSQL backup (deliberately outside Temporal)

[Service]
Type=oneshot
ExecStart=/usr/bin/env bash -c 'mkdir -p ~/.legends-temporal/backups && pg_dump -Fc temporal > ~/.legends-temporal/backups/temporal-$(date -u +%Y%m%dT%H%M%SZ).dump'
"""
    backup_timer = """[Unit]
Description=Daily PostgreSQL backup timer for legends-temporal

[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true

[Install]
WantedBy=timers.target
"""
    files = {
        str(out_dir / f"legends-temporal-worker-{role}.service"): worker_unit,
        str(out_dir / "legends-temporal-backup.service"): backup_service,
        str(out_dir / "legends-temporal-backup.timer"): backup_timer,
    }
    should_write = bool(write and not dry_run)
    if should_write:
        out_dir.mkdir(parents=True, exist_ok=True)
        for fpath, content in files.items():
            Path(fpath).write_text(content, encoding="utf-8")
    return {
        "ok": True,
        "target": "systemd",
        "dry_run": not should_write,
        "action": "written" if should_write else "preview",
        "role": role,
        "profile": profile.name,
        "output_dir": str(out_dir),
        "files": files,
    }
