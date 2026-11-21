# SPDX-License-Identifier: MIT
"""Connection wrapper over temporalio.client.Client plus HTTP UI and SSH probes.

Every operation in this package returns plain JSON-serializable dicts.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Profile

NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW

# Exit codes (see docs/CLI-CONTRACT.json)
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_WORKFLOW_FAILED = 4
EXIT_CONNECT = 5


class LegendsTemporalError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = EXIT_ERROR):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": self.code, "message": self.message}


def cluster_safe(exc: BaseException) -> str:
    """Short, single-line error text safe to show an agent (no stack, bounded)."""
    return f"{type(exc).__name__}: {' '.join(str(exc).split())[:300]}"


def require_temporalio():
    try:
        import temporalio  # noqa: F401
        return temporalio
    except ImportError as exc:  # pragma: no cover - exercised only without SDK
        raise LegendsTemporalError(
            "SDK_MISSING", "temporalio is not installed: python -m pip install temporalio", EXIT_USAGE) from exc


def _read_bytes(path: str) -> bytes:
    return Path(path).expanduser().read_bytes()


async def connect(profile: Profile, identity: str | None = None, timeout_s: float = 10.0):
    """Connect to Temporal with optional TLS/mTLS/API key. Raises LegendsTemporalError(CONNECT_FAILED)."""
    require_temporalio()
    from temporalio.client import Client
    from temporalio.service import TLSConfig

    tls: Any = False
    if profile.tls or profile.tls_cert or profile.tls_ca:
        tls = TLSConfig(
            server_root_ca_cert=_read_bytes(profile.tls_ca) if profile.tls_ca else None,
            client_cert=_read_bytes(profile.tls_cert) if profile.tls_cert else None,
            client_private_key=_read_bytes(profile.tls_key) if profile.tls_key else None,
            domain=profile.tls_server_name or None,
        )
    api_key = profile.api_key()
    if api_key and tls is False:
        tls = True  # API keys always travel over TLS
    kwargs: dict[str, Any] = {"namespace": profile.namespace, "tls": tls}
    if api_key:
        kwargs["api_key"] = api_key
    if identity:
        kwargs["identity"] = identity
    try:
        return await asyncio.wait_for(Client.connect(profile.address, **kwargs), timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        raise LegendsTemporalError("CONNECT_TIMEOUT", f"no answer from {profile.address} in {timeout_s}s",
                                   EXIT_CONNECT) from exc
    except Exception as exc:  # noqa: BLE001 - SDK raises several RuntimeError shapes
        raise LegendsTemporalError("CONNECT_FAILED", f"{profile.address}: {exc}", EXIT_CONNECT) from exc


def run(coro):
    return asyncio.run(coro)


# ---------- conversion helpers ----------

def iso(value: Any) -> str | None:
    """datetime or protobuf Timestamp -> ISO-8601 UTC string."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if hasattr(value, "seconds") and hasattr(value, "nanos"):
        if not value.seconds and not value.nanos:
            return None
        dt = datetime.fromtimestamp(value.seconds + value.nanos / 1e9, tz=timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    return str(value)


def seconds(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, timedelta):
        return value.total_seconds()
    if hasattr(value, "seconds") and hasattr(value, "nanos"):
        return value.seconds + value.nanos / 1e9
    return float(value)


def decode_payloads(payloads: Any) -> list[Any]:
    """Decode a Payloads protobuf (or list of Payload) with the default JSON converter."""
    if payloads is None:
        return []
    items = list(getattr(payloads, "payloads", payloads))
    if not items:
        return []
    from temporalio.converter import default
    conv = default().payload_converter
    out = []
    for p in items:
        try:
            out.append(conv.from_payloads([p])[0])
        except Exception:  # noqa: BLE001
            out.append({"undecodable_payload_bytes": len(getattr(p, "data", b""))})
    return out


def jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, timedelta):
        return value.total_seconds()
    if hasattr(value, "__dataclass_fields__"):
        return {k: jsonable(getattr(value, k)) for k in value.__dataclass_fields__}
    if hasattr(value, "name") and hasattr(value, "value"):  # enums
        return value.name
    return str(value)


def dumps(value: Any, pretty: bool = True) -> str:
    return json.dumps(jsonable(value), indent=2 if pretty else None, ensure_ascii=False, sort_keys=False)


# ---------- non-gRPC probes ----------

def _pinned_get(url: str, ip: str, timeout_s: float) -> int:
    """GET url while connecting to `ip` (TLS SNI and Host stay the URL's hostname)."""
    import http.client
    import socket as _socket
    import ssl
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    raw = _socket.create_connection((ip, port), timeout=timeout_s)
    if parts.scheme == "https":
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        conn = http.client.HTTPSConnection(host, port, timeout=timeout_s)
    else:
        sock = raw
        conn = http.client.HTTPConnection(host, port, timeout=timeout_s)
    conn.sock = sock
    try:
        conn.request("GET", parts.path or "/", headers={"User-Agent": "legends-temporal-doctor"})
        return conn.getresponse().status
    finally:
        conn.close()


def ui_status(url: str, timeout_s: float = 8.0, resolve_ip: str = "") -> dict[str, Any]:
    if not url:
        return {"checked": False, "reason": "no ui_url configured"}
    if resolve_ip:
        try:
            status = _pinned_get(url, resolve_ip, timeout_s)
            return {"checked": True, "url": url, "via": resolve_ip, "status": status,
                    "ok": 200 <= status < 400 or status in (401, 403)}
        except Exception as exc:  # noqa: BLE001
            return {"checked": True, "url": url, "via": resolve_ip, "ok": False, "error": str(exc)[:200]}
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "legends-temporal-doctor"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - operator-configured URL
            return {"checked": True, "url": url, "status": resp.status, "ok": 200 <= resp.status < 400}
    except urllib.error.HTTPError as exc:
        return {"checked": True, "url": url, "status": exc.code, "ok": exc.code in (401, 403)}
    except Exception as exc:  # noqa: BLE001
        return {"checked": True, "url": url, "ok": False, "error": str(exc)[:200]}


def ssh_systemd(host: str, units: list[str], user_units: bool = True, ssh_bin: str = "ssh",
                timeout_s: float = 20.0) -> dict[str, Any]:
    """Read-only `systemctl is-active` over SSH (BatchMode, no password prompts)."""
    if not host or not units:
        return {"checked": False, "reason": "no ssh host/units configured"}
    scope = "--user " if user_units else ""
    remote = " ; ".join(f"echo {u}=$(systemctl {scope}is-active {u})" for u in units)
    cmd = [ssh_bin, "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, remote]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, creationflags=NO_WINDOW)
    except FileNotFoundError:
        return {"checked": True, "ok": False, "error": f"ssh binary not found: {ssh_bin}"}
    except subprocess.TimeoutExpired:
        return {"checked": True, "ok": False, "error": "ssh timed out"}
    states: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            states[k.strip()] = v.strip()
    ok = proc.returncode in (0, 3) and bool(states) and all(v == "active" for v in states.values())
    out: dict[str, Any] = {"checked": True, "host": host, "units": states, "ok": ok}
    if not states:
        out["error"] = (proc.stderr or "no output")[-300:]
    return out
