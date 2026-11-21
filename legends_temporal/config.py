# SPDX-License-Identifier: MIT
"""Profile and environment resolution.

Precedence (highest first):
  1. CLI flags (--address, --namespace)
  2. Environment (TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE, TEMPORAL_TLS_*)
  3. Selected profile from the config file (--profile, LEGENDS_TEMPORAL_PROFILE,
     or the file's "default_profile")
  4. Auto-detected profile: the first profile with "detect": true whose address
     answers a TCP probe (only when no profile was selected explicitly)
  5. Portable defaults (localhost:7233, namespace "default")

Config file: --config, LEGENDS_TEMPORAL_CONFIG, or ~/.legends-temporal/config.json.
No user-specific paths, hosts or secrets live in this module.
"""
from __future__ import annotations

import json
import os
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ENV_CONFIG = "LEGENDS_TEMPORAL_CONFIG"
ENV_PROFILE = "LEGENDS_TEMPORAL_PROFILE"
DEFAULT_CONFIG_PATH = Path.home() / ".legends-temporal" / "config.json"


class ConfigError(ValueError):
    """Invalid configuration (exit code 2)."""


@dataclass
class NotifyConfig:
    """How a worker posts failure notices. kind: stdout | buzz | webhook | none."""
    kind: str = "stdout"
    buzz_exe: str = "buzz"
    buzz_relay: str = ""
    buzz_identity_file: str = ""          # JSON file with {"private_key": ...}; read at call time
    channels: dict[str, str] = field(default_factory=dict)  # friendly name -> channel id
    webhook_url_env: str = "LEGENDS_TEMPORAL_WEBHOOK_URL"  # URL read from env, never stored


@dataclass
class SSHConfig:
    host: str = ""                        # user@host for systemd inspection (optional)
    ssh_bin: str = "ssh"
    units: list[str] = field(default_factory=list)
    user_units: bool = True


@dataclass
class Profile:
    name: str = "portable"
    address: str = "localhost:7233"
    namespace: str = "default"
    api_key_env: str = "TEMPORAL_API_KEY"   # name of env var holding an API key (never the key)
    tls: bool = False
    tls_cert: str = ""
    tls_key: str = ""
    tls_ca: str = ""
    tls_server_name: str = ""
    workflow_queue: str = "legends-temporal"   # where ScriptJob workflows run (control worker)
    exec_queue: str = "legends-temporal"       # where scripts execute (exec worker)
    watch_queues: list[str] = field(default_factory=list)
    interpreter: str = "python"                # interpreter path on the exec worker machine
    cwd: str = ""
    agent_ctl: str = ""                        # path to agent_ctl.py on the exec machine
    stipend_script: str = ""                   # path to stipend router script on the exec machine
    agent_ids: dict[str, str] = field(default_factory=dict)        # alias -> agent controller id
    excluded_agents: dict[str, str] = field(default_factory=dict)  # alias -> exclusion reason
    model_aliases: dict[str, str] = field(default_factory=dict)    # model alias -> canonical model id
    sessions: dict[str, str] = field(default_factory=dict)         # alias -> agent session id
    timezone: str = "UTC"
    notify_channel: str = "ops"
    ui_url: str = ""
    ui_resolve: str = ""                       # optional IP to connect to for ui_url (keeps TLS hostname)
    detect: bool = False
    jobs_source: str = ""                      # module:ATTR of a Python JOBS inventory
    canaries_source: str = ""
    jobs_pythonpath: list[str] = field(default_factory=list)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    ssh: SSHConfig = field(default_factory=SSHConfig)
    source: str = "defaults"

    def api_key(self) -> str | None:
        val = os.environ.get(self.api_key_env or "", "")
        return val or None

    def public_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["api_key_present"] = bool(self.api_key())
        return d


_PROFILE_KEYS = set(Profile.__dataclass_fields__) - {"notify", "ssh", "source", "name"}
_LEGACY_CTL_KEY = "omni" + "gent_ctl"


def config_path(explicit: str | None = None, env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if explicit:
        return Path(explicit).expanduser()
    if env.get(ENV_CONFIG):
        return Path(env[ENV_CONFIG]).expanduser()
    return DEFAULT_CONFIG_PATH


def load_config_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config file {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config file {path} must be a JSON object")
    if not isinstance(data.get("profiles", {}), dict):
        raise ConfigError("'profiles' must be an object of name -> profile")
    return data


def profile_from_dict(name: str, raw: dict[str, Any], source: str) -> Profile:
    raw = dict(raw)
    if _LEGACY_CTL_KEY in raw:
        raw.setdefault("agent_ctl", raw.pop(_LEGACY_CTL_KEY))
    unknown = set(raw) - _PROFILE_KEYS - {"notify", "ssh", "description"}
    if unknown:
        raise ConfigError(f"profile '{name}' has unknown keys: {sorted(unknown)}")
    kwargs = {k: v for k, v in raw.items() if k in _PROFILE_KEYS}
    prof = Profile(name=name, source=source, **kwargs)
    try:
        if isinstance(raw.get("notify"), dict):
            prof.notify = NotifyConfig(**raw["notify"])
        if isinstance(raw.get("ssh"), dict):
            prof.ssh = SSHConfig(**raw["ssh"])
    except TypeError as exc:
        raise ConfigError(f"profile '{name}': {exc}") from exc
    validate_profile(prof)
    return prof


def validate_profile(p: Profile) -> None:
    if ":" not in p.address:
        raise ConfigError(f"address '{p.address}' must be host:port")
    if not p.namespace:
        raise ConfigError("namespace must not be empty")
    if p.notify.kind not in {"stdout", "buzz", "webhook", "none"}:
        raise ConfigError(f"notify.kind '{p.notify.kind}' must be stdout|buzz|webhook|none")
    if bool(p.tls_cert) != bool(p.tls_key):
        raise ConfigError("mTLS needs both tls_cert and tls_key")


def tcp_reachable(address: str, timeout: float = 1.5) -> bool:
    host, _, port = address.rpartition(":")
    try:
        with socket.create_connection((host.strip("[]"), int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def resolve_profile(
    profile: str | None = None,
    config: str | None = None,
    address: str | None = None,
    namespace: str | None = None,
    env: dict[str, str] | None = None,
    probe=tcp_reachable,
) -> Profile:
    env = os.environ if env is None else env
    path = config_path(config, env)
    data = load_config_file(path)
    profiles: dict[str, Any] = data.get("profiles", {})
    wanted = profile or env.get(ENV_PROFILE) or data.get("default_profile")

    if wanted:
        if wanted not in profiles:
            raise ConfigError(f"profile '{wanted}' not found in {path}")
        prof = profile_from_dict(wanted, profiles[wanted], f"config:{path}")
    else:
        prof = Profile()
        for name, raw in profiles.items():
            if isinstance(raw, dict) and raw.get("detect") and raw.get("address") and probe(raw["address"]):
                prof = profile_from_dict(name, raw, f"detected:{path}")
                break

    if env.get("TEMPORAL_ADDRESS"):
        prof.address = env["TEMPORAL_ADDRESS"]
    if env.get("TEMPORAL_NAMESPACE"):
        prof.namespace = env["TEMPORAL_NAMESPACE"]
    for key, attr in (("TEMPORAL_TLS_CERT", "tls_cert"), ("TEMPORAL_TLS_KEY", "tls_key"),
                      ("TEMPORAL_TLS_CA", "tls_ca"), ("TEMPORAL_TLS_SERVER_NAME", "tls_server_name")):
        if env.get(key):
            setattr(prof, attr, env[key])
            prof.tls = True
    if env.get("TEMPORAL_TLS", "").lower() in {"1", "true", "yes"}:
        prof.tls = True
    if address:
        prof.address = address
    if namespace:
        prof.namespace = namespace
    if not prof.watch_queues:
        prof.watch_queues = sorted({prof.workflow_queue, prof.exec_queue})
    validate_profile(prof)
    return prof
