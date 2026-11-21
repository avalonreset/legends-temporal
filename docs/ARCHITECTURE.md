# Architecture

```text
            agent / human
                 |
        legends-temporal CLI | MCP (stdio)
                 |  gRPC (TLS, mTLS or API key optional)
                 v
        +----------------------+
        |  Temporal frontend   |  schedules, timers, history, visibility
        |  (+ Postgres)        |
        +----------------------+
           |               |
  workflow_queue        exec_queue (one or many)
  (control plane)       (exec plane)
           |               |
   worker --role control   worker --role exec
   ScriptJob workflow      run_script, notify_buzz
   log_failure             (machine with the data)
```

## Modules

| Module | Responsibility |
| --- | --- |
| `config` | Profile dataclasses, config file loading, strict key validation, resolution order, TCP detection probe |
| `client` | Connect (TLS, mTLS, API key), error type and exit codes, JSON helpers, payload decoding, UI and SSH probes |
| `runtime` | The `ScriptJob` wire contract, job builders, notifier, script executor, activities, workflow, worker factory |
| `timers` | Delay grammar, one-shot job construction, `start_delay` start |
| `schedules` | Definition loading (JSON, YAML, Python inventory), schedule build, apply, list, describe and lifecycle operations |
| `workflows` | Visibility query builder, list, describe, history, diagnosis, start, signal, query, cancel, terminate, job runs |
| `queues` | Poller inspection and health |
| `namespaces` | Namespace listing, description and cluster info |
| `doctor` | Offline and live readiness checks, resource paths |
| `mcp_server` | Newline-delimited JSON-RPC 2.0 MCP server over stdio |
| `cli` | Argparse tree, dispatch, human renderers, exit code mapping |

## The ScriptJob contract

The workflow input is a dataclass with exactly 11 fields (see
`docs/CLI-CONTRACT.json` `wire_contract`). `runtime.to_wire()` is the only
producer: it drops unknown keys, fills defaults and requires `job_id`, `title`,
`queue`, `interpreter` and `script`. Workers built from older or newer releases
interoperate as long as this set does not change.

Workflow logic:

1. `run_script` on `job.queue`: `schedule_to_start = wait_for_worker_s`,
   `start_to_close = timeout_s + 60s`, one attempt.
2. Success returns `{ok, job, exit_code, duration_s}`.
3. Failure (non-zero exit, timeout, no worker) runs `log_failure` on the
   workflow's own queue, then `notify_buzz` on `job.queue`, then raises a
   non-retryable `ApplicationError` so the run is `FAILED`.

The activity name `notify_buzz` is kept for wire compatibility. Its
implementation dispatches to the profile notifier (`stdout`, `webhook`, `buzz`
or `none`).

## Script execution

`execute_script` runs `[interpreter, script, *args]` with `shell=False`, the
job `cwd`, a hard timeout (exit 124) and, on Windows, `CREATE_NO_WINDOW` so
scheduled jobs never flash a console. A missing interpreter or script is exit
127. Output is merged and the last 1500 characters are kept as the tail.

## Timeouts

- Schedule actions: `execution_timeout = wait_for_worker_s + timeout_s + 3h`.
- Timers: `execution_timeout = delay + wait_for_worker_s + timeout_s + 1h`.

## Config resolution

1. `--address`, `--namespace` flags.
2. `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`, `TEMPORAL_API_KEY` (or the
   profile's `api_key_env`), `TEMPORAL_TLS_CERT|KEY|CA`.
3. `--profile NAME`, else the file's `default_profile`.
4. A profile with `detect: true` whose address answers a TCP probe.
5. Built-in portable default: `localhost:7233`, namespace `default`, queues
   `legends-temporal`, interpreter `python`, timezone `UTC`, stdout notices.

`config show` prints the resolved profile with secrets reduced to presence
flags.

## Determinism

The workflow imports only `runtime` dataclasses through the sandbox
passthrough. It reads no clock, environment or file. All side effects are
activities.

## Errors

Every failure becomes `LegendsTemporalError(code, message, exit_code)` and is
printed as JSON. gRPC `NOT_FOUND` maps to exit 3, connect failures to exit 5,
failed workflows under `--wait` to exit 4.
