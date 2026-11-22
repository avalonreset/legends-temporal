# `legends-temporal` agent recipe (canonical)

Module `legends-temporal` v0.1.1. Agent-operated Temporal: durable schedules,
one-shot timers, first-class `cto-legends` `--module` scheduling, orphaned-queue
and declarative drift auditing (`audit`), local/Docker/systemd cluster bootstrap
(`bootstrap`), zero-rewrite script and agent jobs, queue and namespace health,
and history diagnosis, over the official `temporalio` Python SDK.

The only registered skill is `cto-legends`. This file is the canonical task
instruction for the module. No standalone module skill exists or is required.
An optional stdio MCP server (`legends-temporal mcp`) exposes 9 read/preview tools
and 1 confirm-gated timer tool.

Read first, in order:

1. `docs/CLI-CONTRACT.json` (exact CLI syntax, exit codes, wire contract)
2. `docs/KNOWLEDGE-PACK.md`, then the one note in `knowledge/temporal/` that matches the task
3. `docs/TROUBLESHOOTING.md` when anything fails
4. `docs/INTEGRATIONS.md` when wiring agents, webhooks or an existing job inventory

## Runtime shape

- Entries: `legends-temporal` console script and `python -m legends_temporal`.
- Python 3.11+ with `temporalio>=1.8` (tested on 1.36.1). No `temporal` CLI needed.
- Talks gRPC to a Temporal frontend. Config comes from flags, then env, then a
  named profile in `~/.legends-temporal/config.json`, then a detected profile,
  then the portable default `localhost:7233` / `default`.
- Ships one workflow type, `ScriptJob`, and a worker (`legends-temporal worker`)
  that hosts it. Existing workers that already implement the same 11-field
  `ScriptJob` contract work unchanged.

## Readiness

1. `legends-temporal doctor --json` (offline: Python, SDK, config, recipe and pack files).
2. `legends-temporal doctor --live --json` (gRPC connect, namespace, every
   `watch_queues` poller, optional UI HTTP and SSH unit checks).
3. A task is ready only when the live doctor shows `ok: true` and the queues the
   task needs report live pollers. A registered guide or a passing offline
   doctor is not proof of live readiness.

## Task map

| User intent | Command |
| --- | --- |
| Start a local persisted dev server or generate a hardened Docker Compose stack | `bootstrap local [--start]`, `bootstrap compose [--write]`, `bootstrap systemd [--write]` |
| Is Temporal up, what is running | `doctor --live`, `queue describe`, `namespace list` |
| Audit schedules for orphaned queues, leaked secrets, or inventory drift | `audit`, `audit --file defs.json`, `audit --file defs.json --offline` |
| What is scheduled | `schedule list`, `schedule describe <id>` |
| Schedule another `cto-legends` module on a cron or timer | `schedule create --id ... --cron "..." --module legends-<name> --arg=... --dry-run` or `timer in <delay> --module legends-<name> --arg=... --dry-run` |
| Run this later, once | `timer in <delay> --code ... \| --script ... \| --module ... \| --session <id> --message ... \| --command ... \| --webhook-env NAME` |
| Run this every day/hour | `schedule create --id ... --cron "..." ...` or `schedule apply --file defs.json` |
| Run a defined job now | `job run <id>` or `schedule trigger <id>` |
| Why did it fail | `workflow list --status failed`, then `workflow history <id> --diagnose` |
| Stop a schedule | `schedule pause <id> --note "..."` (reversible) before `schedule delete <id> --yes` |
| Cancel a pending timer | `workflow cancel <id>` |

Always add `--json` when parsing output. Preview every mutation first:
`timer in ... --dry-run`, `schedule create ... --dry-run`, `schedule apply --dry-run`.

## Authority

- Read commands are safe to run for any task.
- Mutations (see `mutating` in the contract) need the user's request for that
  exact job. `schedule delete` and `workflow terminate` also need `--yes`.
- Never put secrets in job args, messages or webhook bodies: they land in
  Temporal history. Use env vars or files on the exec worker.
- Never replace Temporal with a host scheduler (cron, Task Scheduler, a vendor
  agent scheduler) for jobs that this module covers.

## Exit codes

`0` ok, `1` error, `2` usage, `3` not found, `4` workflow failed, `5` cannot connect.
Errors print `{"ok": false, "error": "<CODE>", "message": "..."}` JSON on stdout.

## Receipts

After a mutation, report the workflow or schedule id, the queue, and the
`--json` result. For failures include the `--diagnose` verdict fields
`status`, `script_exit_code`, `error` and `hint`.
