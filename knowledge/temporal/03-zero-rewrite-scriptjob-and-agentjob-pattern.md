# 03. Zero-rewrite ScriptJob and AgentJob pattern

Most automation already exists as scripts and agent prompts. Rewriting them as Temporal workflows is slow and risky. The ScriptJob pattern wraps them unchanged.

## The ScriptJob contract

One workflow type, `ScriptJob`, takes one dataclass payload with exactly these 11 fields:

| Field | Meaning |
| --- | --- |
| `job_id` | Stable job name (schedule id or `oneshot-<mode>-<epoch>`). |
| `title` | Human label used in notices. |
| `queue` | Exec queue: where `run_script` and the notice run (the machine that has the data). |
| `interpreter` | Executable, for example `python`, `node`, `bash`, `pwsh`, or an absolute path. |
| `script` | Script path, or `-c` for inline code. |
| `args` | List of string arguments. |
| `cwd` | Working directory on the exec machine. Empty means the worker's cwd. |
| `timeout_s` | Hard kill for the script. Exit code 124 on timeout. |
| `wait_for_worker_s` | How long the activity may wait in the queue for an offline machine (schedule-to-start). |
| `notify_channel` | Channel for the failure notice. Empty disables it. |
| `cron` | Informational copy of the cron spec, shown in notices. |

The workers reject unknown keys, so `legends_temporal.runtime.to_wire()` always emits exactly this set. Keep this contract stable: deployed workers of any version can run jobs built by any version of the CLI.

## Workflow logic

1. Execute activity `run_script` on `job.queue` with `schedule_to_start = wait_for_worker_s` and `start_to_close = timeout_s + 60s`, one attempt.
2. Exit 0: complete with `{ok, job, exit_code, duration_s}`.
3. Non-zero exit, timeout or the worker never arrived: run `log_failure` on the workflow's own queue (always-on control plane), then `notify_buzz` on `job.queue` with the failure text and output tail, then fail with a non-retryable `ApplicationError`.

Failure notices are sent once per failed run. Scripts are never retried inside a run (see note 04); the next schedule tick is the retry.

## Split control and exec planes

- **Control queue** (profile `workflow_queue`) hosts the workflow and `log_failure`. Put it on an always-on server.
- **Exec queue** (profile `exec_queue`, or per job `queue`) hosts `run_script` and the notifier. Put it on the machine that has the files, GPUs, browser sessions or credentials.

A laptop that sleeps is fine as an exec machine: the activity waits up to `wait_for_worker_s` in the queue and runs when it wakes.

## Job builders

| Builder | CLI | What runs |
| --- | --- | --- |
| `script_job` | `--script path --args ...` | `interpreter path args...` |
| `code_job` | `--code "..."` | `interpreter -c "..."` |
| `agent_job` | `--session <id> --message "..." [--model m]` | `interpreter agent_ctl send <id> --message ... [--model m]`, which delivers a turn to an agent session. Temporal owns the clock and the agent does the reasoning. |
| `command_job` | `--command argv...` | `python -c` wrapper that runs the argv with `subprocess.call` (no shell). |
| `webhook_job` | `--webhook-env NAME [--body json]` | POSTs the JSON body to the URL stored in env var `NAME` on the exec worker. The URL never enters Temporal history. |

## Python job inventories

Existing projects often keep jobs as Python dicts (`JOBS = {...}`). A profile can point at them with `jobs_source: "package.module:JOBS"` plus `jobs_pythonpath`. `schedule apply` and `job list|run` then read that inventory directly, so the live definitions keep a single source of truth.
