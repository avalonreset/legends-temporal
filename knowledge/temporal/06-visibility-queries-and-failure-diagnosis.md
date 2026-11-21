# 06. Visibility queries and failure diagnosis

## Visibility list filter

`workflow list` uses Temporal's SQL-like visibility query. The module builds common filters for you:

| Flag | Query clause |
| --- | --- |
| `--status failed` | `ExecutionStatus="Failed"` |
| `--type ScriptJob` | `WorkflowType="ScriptJob"` |
| `--id-prefix canary-` | `WorkflowId STARTS_WITH "canary-"` |
| `--query "..."` | Your raw clause, ANDed with the above |

Useful raw clauses: `StartTime > '2026-01-01T00:00:00Z'`, `CloseTime BETWEEN ... AND ...`, `TaskQueue = 'ops'`, `ExecutionStatus = 'Running' AND StartTime < '...'` (stuck runs). Statuses: `Running`, `Completed`, `Failed`, `Canceled`, `Terminated`, `ContinuedAsNew`, `TimedOut`. Schedule-started runs also carry `TemporalScheduledById`, so `TemporalScheduledById = 'my-schedule'` finds every run of one schedule.

On Postgres visibility, `STARTS_WITH` and the standard keyword attributes work. Advanced search attributes need Elasticsearch or a recent SQL visibility store.

## Reading a history

`workflow history <id>` prints raw events. `--diagnose` turns them into a verdict:

- `status` and the terminal event.
- The `run_script` exit code, attempt count and which worker identity ran it.
- The failed activity and its error message, if any.
- The script's output tail (from the activity result or the failure notice).
- Whether `log_failure` ran and whether a notice was posted.
- A timeline of the key events with offsets from the start.
- A `hint` for the common failure shapes.

It exits 0 even when the workflow failed, because the diagnosis itself succeeded. Read `verdict.ok`.

## Triage recipe

1. `legends-temporal workflow list --status failed --limit 10` to find recent failures.
2. `legends-temporal workflow history <id> --diagnose` on each.
3. Match the verdict:
   - `script_exit_code` non-zero with an output tail: the script failed. Fix the script; Temporal did its job.
   - Timeout type `SCHEDULE_TO_START`: no exec worker. Run `queue describe <exec-queue>`.
   - `START_TO_CLOSE`: the worker died or slept mid-run. Check the worker log on that machine.
   - Exit 127: wrong interpreter or path.
   - No `run_script` scheduled at all, and the run is `RUNNING`: no control worker, or a timer that has not fired yet. Check `queue describe <workflow-queue>` and the start delay in `workflow describe`.
4. Rerun after the fix with `job run <id>` or `schedule trigger <schedule-id>`.

## Pollers

`queue describe <q>` lists workflow and activity pollers with identity and last access time. The module calls a poller live when it was seen in the last 120 seconds. Healthy means at least one live poller of the kind the queue needs. Identities look like `<name>@<HOST>` so you can tell which machine is serving.
