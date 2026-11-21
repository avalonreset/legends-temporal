# Integrations

## Agents through cto-legends

Agents find the module through the `cto-legends` skill:

```bash
cto-legends handoff legends-temporal
```

That returns `docs/AGENT-RECIPE.md`. No per-module skill is installed.

## MCP server

```json
{
  "mcpServers": {
    "legends-temporal": {"command": "legends-temporal", "args": ["mcp"]}
  }
}
```

Add `"--profile", "NAME"` to `args` to pin a profile. Tools:

| Tool | Effect |
| --- | --- |
| `temporal_doctor` | Readiness; live by default, `live: false` for offline only |
| `temporal_namespace_list` | Namespaces with state and retention |
| `temporal_queue_describe` | Pollers and health for one task queue (`task_queue`) |
| `temporal_schedule_list` | Schedules with spec, next run and paused state |
| `temporal_schedule_describe` | One schedule in full |
| `temporal_workflow_list` | Executions filtered by status, type, id prefix or raw query |
| `temporal_workflow_diagnose` | History verdict for one execution |
| `temporal_timer_schedule` | One-shot job; returns a preview unless `confirm: true` |

Only `temporal_timer_schedule` can change anything, and only with `confirm`.

## Agent session turns (AgentJob)

`--session <id> --message "..."` builds a job that runs the profile's
`agent_ctl` script with `send <id> --message ... [--model m]`. Temporal owns
the clock and the agent does the reasoning:

```bash
legends-temporal timer in 2h --session <session-id> --message "Check the deploy and report."
legends-temporal schedule create --id weekly-review --cron "0 9 * * 1" \
  --session <session-id> --message "Weekly review."
```

Any CLI that delivers a message to an agent can stand in for `agent_ctl` as
long as it accepts `send <id> --message <text> [--model <m>]`. Otherwise use
`--script` or `--command` with your own wrapper.

## Existing Python job inventories

Projects that already keep jobs in Python (`JOBS = {"id": {...}}` with the
ScriptJob fields plus schedule keys) can be used directly:

```json
"jobs_source": "mypkg.jobs:JOBS",
"canaries_source": "mypkg.jobs:CANARIES",
"jobs_pythonpath": ["/path/to/project"]
```

Then `schedule apply` (no `--file`), `job list` and `job run <id> [--canary]`
read that inventory. Or pass `--from-python mypkg.jobs:JOBS --pythonpath ...`
once.

## Webhooks

```bash
legends-temporal timer in 10m --webhook-env DEPLOY_HOOK --body '{"ref": "main"}'
```

The exec worker reads the URL from env var `DEPLOY_HOOK` at run time and POSTs
the JSON body. The URL is never stored in Temporal history.

## Notifiers

Failure notices go through the profile's `notify` block:

| `kind` | Behaviour |
| --- | --- |
| `stdout` | Prints the notice in the worker log (default) |
| `webhook` | POSTs `{"channel", "text"}` to the URL in env var `webhook_url_env` |
| `buzz` | Posts to a Buzz channel with the Buzz CLI (`buzz_exe`, `buzz_relay`, `buzz_identity_file`, `channels` name to id map). The identity file is read at call time and never logged. |
| `none` | Disabled |

A job's `notify_channel` picks the channel; an empty value disables the notice
for that job.

## Coexisting with an existing ScriptJob deployment

If workers already serve a `ScriptJob` workflow with the same 11-field
contract, point a profile at their queues (`workflow_queue`, `exec_queue`) and
use this CLI as the operator surface. Nothing on the workers needs to change.
Replace those workers with `legends-temporal worker` only when you choose to.

## Host schedulers

Do not mirror Temporal schedules into cron, Task Scheduler or vendor agent
schedulers. One clock per job. Use `legends-temporal schedule list` as the
inventory of record.
