# 02. Schedules and durable timers

Temporal replaces cron, Windows Task Scheduler, systemd timers and `sleep && run` with two server-side primitives. Both survive restarts of every client and worker, because the clock lives in the server.

## Schedules (recurring)

A Schedule is a server object that starts a workflow on a spec. Key fields, all exposed by `legends-temporal schedule create` and `schedule apply`:

| Field | Meaning | Module default |
| --- | --- | --- |
| `cron_expressions` | Standard 5-field cron. Temporal stores them as calendar specs and `schedule list` renders them back as cron. | none |
| `intervals` | Every N (for example `--interval 15m`), optional offset. | none |
| `time_zone_name` | IANA zone for cron evaluation, DST aware. | profile `timezone` (portable: `UTC`) |
| `jitter` | Random delay up to this amount per action. | 0 |
| `overlap` | What to do if the previous run is still going: `SKIP`, `BUFFER_ONE`, `BUFFER_ALL`, `CANCEL_OTHER`, `TERMINATE_OTHER`, `ALLOW_ALL`. | `SKIP` |
| `catchup_window` | How far back missed actions are replayed after server downtime. | `12h` |
| `remaining_actions` | Run N times, then stop (limited schedules). | unlimited |
| `paused` + `note` | Paused schedules keep their spec but start nothing. | running |

Operations: `trigger` (run now, honours overlap), `pause` and `unpause` with a note, `backfill` (replay a past time range), `delete` (needs `--yes`). Schedule ids are stable names; `apply` creates a missing schedule and updates an existing one in place, so re-applying the same definition file is idempotent.

Each run gets a workflow id of `<schedule-id>-<RFC3339 time>`, which is why `workflow list --id-prefix ops-` groups runs by schedule.

## Durable timers (one-shot)

`legends-temporal timer in <delay>` starts a `ScriptJob` workflow with `start_delay`. The server holds the start until the delay passes, then dispatches. Properties:

- Delay grammar: `90`, `90s`, `15m`, `2h`, `1d`, `1w`, combos like `1h30m`. Plain digits are seconds. Maximum 366 days.
- Survives reboots of the machine that asked. Nothing is sleeping locally.
- Shows up immediately in `workflow list` with status `RUNNING` and no history beyond the start event until it fires.
- Cancel before it fires with `workflow cancel <id>` or `workflow terminate <id> --yes`.
- `--wait` blocks until the job finishes and returns the result (exit 4 if it failed).

Inside a workflow, `await workflow.sleep(...)` or `asyncio.sleep(...)` creates a durable timer too. This module does not need one, because `start_delay` covers the one-shot case without custom workflow code.

## Choosing

- Repeats on a calendar: Schedule with cron and timezone.
- Repeats every N minutes regardless of the clock: Schedule with `--interval`.
- Runs once later: `timer in`.
- Runs once now and you want the result: `job run <id>` or `workflow start ScriptJob --wait`.
- Runs N times then stops: Schedule with `--remaining-actions N`.

## Execution timeouts set by the module

- Schedule action: `wait_for_worker_s + timeout_s + 3h`.
- Timer: `delay + wait_for_worker_s + timeout_s + 1h`.

These bound the whole execution so a job whose worker never returns is eventually closed as `TIMED_OUT` instead of running forever.
