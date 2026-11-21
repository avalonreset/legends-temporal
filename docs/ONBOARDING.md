# Onboarding

From nothing to a durable scheduled job.

## 1. Get a Temporal server

Pick one:

- **Local dev:** install the Temporal CLI and run `temporal server start-dev`.
  Frontend `localhost:7233`, UI `http://localhost:8233`, namespace `default`.
  State is in memory unless you pass `--db-filename`.
- **Self-hosted:** Temporal with PostgreSQL on a private network. Read
  `knowledge/temporal/05-self-hosted-postgres-and-tailnet-hardening.md` before
  exposing anything.
- **Temporal Cloud:** note your namespace (`name.account`), endpoint
  (`name.account.tmprl.cloud:7233`) and create an API key.

## 2. Install and configure

```bash
pip install -e .
legends-temporal config init
legends-temporal config show --json
```

`config init` writes `~/.legends-temporal/config.json` with a `local` profile.
Edit it for a remote server, or skip the file and use env vars:

```bash
export TEMPORAL_ADDRESS=temporal.internal:7233
export TEMPORAL_NAMESPACE=ops
export TEMPORAL_API_KEY=...   # Temporal Cloud only; forces TLS
```

A fuller profile:

```json
{
  "default_profile": "ops",
  "profiles": {
    "ops": {
      "address": "10.0.0.5:7233",
      "namespace": "ops",
      "workflow_queue": "ops-control",
      "exec_queue": "ops-workstation",
      "watch_queues": ["ops-control", "ops-workstation"],
      "interpreter": "python3",
      "cwd": "/srv/jobs",
      "timezone": "Europe/Berlin",
      "notify_channel": "ops",
      "ui_url": "https://temporal.internal:8080/",
      "notify": {"kind": "webhook", "webhook_url_env": "OPS_ALERT_WEBHOOK"},
      "ssh": {"host": "ops@10.0.0.5", "units": ["temporal.service"], "user_units": false}
    }
  }
}
```

Unknown keys are rejected so typos fail loudly.

## 3. Check readiness

```bash
legends-temporal doctor --live
```

Expect PASS for `grpc_connect` and `namespace`. Queue checks fail until a worker
polls them; that is the next step.

## 4. Start workers

One machine, both roles:

```bash
legends-temporal worker --role all
```

Split planes (recommended once you have an always-on server):

```bash
# on the always-on server
legends-temporal worker --role control
# on the machine that has the data
legends-temporal worker --role exec
```

Run workers under a supervisor (systemd, launchd, a Windows service wrapper).
`queue describe` should now show live pollers.

## 5. First jobs

```bash
legends-temporal timer in 30s --code "print('first durable job')" --wait
legends-temporal schedule create --id nightly-report --cron "30 2 * * *" \
  --script scripts/nightly_report.py --arg=--days --arg=1 --dry-run
legends-temporal schedule create --id nightly-report --cron "30 2 * * *" \
  --script scripts/nightly_report.py --arg=--days --arg=1
legends-temporal schedule trigger nightly-report
legends-temporal workflow list --id-prefix nightly-report --limit 3
```

## 6. Keep definitions in a file

Put jobs in a JSON (or YAML with PyYAML) file like `examples/jobs.example.json`
and apply it. Re-applying updates schedules in place:

```bash
legends-temporal schedule apply --file jobs.json --dry-run
legends-temporal schedule apply --file jobs.json
```

## 7. Hand it to your agent

Point the agent at `docs/AGENT-RECIPE.md`, or run `legends-temporal mcp` as a
stdio MCP server. See `docs/INTEGRATIONS.md`.
