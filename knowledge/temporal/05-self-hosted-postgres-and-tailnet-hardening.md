# 05. Self-hosted Temporal on Postgres, private network hardening

A single small server can run Temporal for a whole house or team. The common shape:

- `temporal server` (all services in one process) or the `temporalio/auto-setup` container, backed by **PostgreSQL** for persistence and visibility. SQLite is fine for a dev box but is not durable enough for production schedules.
- Frontend gRPC on `7233`, Web UI on `8080` or `8088`.
- One namespace per environment, with a retention period long enough to debug failures (7 to 30 days).

## Bind privately, never on a public interface

Temporal's frontend has no authentication by default. Anyone who reaches port 7233 can start, terminate and read workflows, including their inputs.

1. Bind the frontend and UI to `127.0.0.1` or to a private overlay address (Tailscale, WireGuard, a VPC subnet). Do not expose `0.0.0.0` on an internet-facing host.
2. Block 7233 and the UI port in the host firewall for public interfaces. Verify from outside with a port check.
3. Put the UI behind something that authenticates (Tailscale Serve inside the tailnet, an SSO proxy, or Temporal's OIDC config).
4. When clients must cross an untrusted network, enable mTLS on the frontend and give each worker its own client cert. This module supports `tls_cert`, `tls_key`, `tls_ca` and `tls_server_name` per profile, or the `TEMPORAL_TLS_*` environment variables.
5. For Temporal Cloud, use an API key (`TEMPORAL_API_KEY`, or the env var named by the profile's `api_key_env`). The module forces TLS whenever an API key is present.

## Keep secrets out of history

Workflow inputs, results and activity payloads are stored in plain form in the database and are visible in the UI. Never put tokens, passwords, webhook URLs or private keys in a job's args. Instead:

- Read secrets from files or env vars on the exec worker at run time (the notifier reads the Buzz identity file at call time; `webhook_job` takes the name of an env var, not the URL).
- Use a payload codec (encryption) if inputs must carry sensitive data.

## Operating the server

- Run each component as a supervised service (systemd user or system units, or containers with restart policies). Check them with `legends-temporal doctor --live`; when a profile has an `ssh` block it also runs `systemctl is-active` on the listed units.
- Back up Postgres. Schedules, history and namespace config all live there.
- Upgrade the server one minor version at a time, following Temporal's schema migration steps.
- Watch `queue describe` for poller freshness. A queue with zero live pollers means work will wait until `wait_for_worker_s` and then fail.

## Name resolution gotcha

On machines with both a LAN and an overlay route, a hostname can resolve to the LAN address while the service only listens on the overlay address. The profile field `ui_resolve` pins the UI health check to a specific IP while keeping the TLS server name, so `doctor` checks the path clients actually use.
