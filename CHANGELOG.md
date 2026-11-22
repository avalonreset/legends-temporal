# Changelog

## 0.1.2 (2026-11-22)

- Sync vendored `cto-legends` router skill pin (`69ddc4f68faa`), enforce LF line endings via `.gitattributes`, and standardize release artifact checksums.

## 0.1.1 (2026-11-21)

- Add first-class `cto-legends` ecosystem module scheduling (`--module <legends-module>` on `schedule create` and `timer in`, `"kind": "module"` in declarative JSON/YAML schedules, and `module` in MCP `temporal_timer_schedule`), compiling any installed `cto-legends` module call into the 11-field `ScriptJob` wire contract (`python -m cto_legends run <module> -- <args>`).
- Add `legends-temporal audit` (and MCP tool `temporal_schedule_audit`) for offline declarative schedule validation (5-field cron syntax, IANA timezones, overlap vs. timeout pile-up detection, and secret/shell-sleep argument scanning) and live cluster audits (cross-checking every schedule's `workflow_queue` and `exec_queue` against live task-queue pollers to catch orphaned queues with 0 active workers, paused schedules, missed catchup windows, and 3-way inventory drift: `in_sync`, `missing_on_server`, `untracked_on_server`, `spec_drifted`).
- Add `legends-temporal bootstrap local|compose|systemd` (and MCP tool `temporal_bootstrap_plan`) to launch SQLite-persisted local Temporal dev servers (`--db-filename` enforced so schedules survive restarts), generate hardened self-hosted PostgreSQL 16 + Temporal + UI Docker Compose stacks bound strictly to `127.0.0.1` or an explicit private IP (refusing `0.0.0.0`), and generate Linux `systemd` worker and daily `pg_dump` backup units.
- Expand the attachable Empire knowledge pack (`knowledge/temporal/pack.json`) to 7 notes with `07-why-not-cron-or-built-in-agent-schedulers.md`.

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
