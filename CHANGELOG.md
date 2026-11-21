# Changelog

## 0.1.0 (2026-11-21)

First public release of `legends-temporal`, agent-operated control of any Temporal cluster.

- CLI `legends-temporal` / `python -m legends_temporal` with `doctor`,
  `namespace`, `queue`, `schedule`, `timer`, `workflow`, `job`, `worker`, `mcp`,
  `agents`, `config` and `resources` commands, JSON output and stable exit codes.
- Profiles: flags, env (`TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`,
  `TEMPORAL_API_KEY`, `TEMPORAL_TLS_*`), named and detected profiles, portable
  `localhost:7233` default. TLS, mTLS and API-key (Temporal Cloud) connections.
- `ScriptJob` workflow and worker with a stable 11-field payload, split control
  and exec task queues, failure log and configurable notices.
- Job builders for scripts, inline code, argv commands, env-held webhooks and
  agent session turns.
- Schedules from flags, JSON or YAML definition files, or an existing Python
  job inventory; idempotent apply, trigger, pause, unpause, backfill, delete.
- Durable one-shot timers with a delay grammar (`90s`, `15m`, `1h30m`, `2d`).
- `workflow history --diagnose`: verdict, exit code, worker, output tail,
  failure log and notice status, timeline and hint.
- Queue poller health, namespace and cluster info, optional UI and SSH unit
  checks in `doctor --live`.
- Stdio MCP server with read tools and a confirm-gated timer tool.
- Knowledge pack `knowledge/temporal` (6 notes, `legends.knowledge-pack/v1`).
