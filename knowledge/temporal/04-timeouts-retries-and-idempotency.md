# 04. Timeouts, retries and idempotency

## The four activity timeouts

| Timeout | Clock starts | Use it for |
| --- | --- | --- |
| `schedule_to_start` | Activity scheduled | Bounding how long work may wait for a worker. ScriptJob sets it to `wait_for_worker_s`, so an offline machine has that long to come back. |
| `start_to_close` | Worker picked it up | Bounding a single attempt. ScriptJob sets it to `timeout_s + 60s`. |
| `schedule_to_close` | Activity scheduled | Bounding all attempts together. |
| `heartbeat` | Last heartbeat | Detecting a hung long-running activity fast. Needs the activity to heartbeat. |

At least `start_to_close` or `schedule_to_close` must be set. Workflow-level `execution_timeout` bounds the whole run including all retries and delays (see note 02 for the module's values).

## Retries

Activities retry by default with exponential backoff and unlimited attempts. That is right for idempotent API calls and wrong for scripts that send messages, charge cards or write files. ScriptJob gives `run_script` exactly one attempt (`maximum_attempts=1`) and treats a non-zero exit as a final answer, not an infrastructure error. The failure log and the notice get 3 attempts each because they are safe to repeat.

Non-retryable errors: raise `ApplicationError(..., non_retryable=True)` to stop retries immediately. ScriptJob ends a failed run this way so the schedule's next tick is the next attempt.

## Idempotency rules for jobs

- **Workflow ids are the first defence.** Temporal refuses to start a second running workflow with the same id. Schedules derive ids from the action time, so a schedule can never double-start the same tick.
- **Overlap policy is the second.** `SKIP` (the module default) drops a tick while the previous run is still going.
- **Scripts must tolerate a rerun.** A worker can crash after the script did its side effect but before the result was recorded. Write output atomically, check before sending, or include a dedupe key such as the workflow id. ScriptJob exports nothing extra into the environment; pass ids as args if the script needs them.
- **Timers are not retried automatically.** If a one-shot job fails, start a new timer or `job run` it.

## Common timeout outcomes and their meaning

| Event | Meaning | Fix |
| --- | --- | --- |
| `ActivityTaskTimedOut` with type `SCHEDULE_TO_START` | No exec worker polled the queue in time | Start the worker, check `queue describe`, raise `wait_for_worker_s` |
| `ActivityTaskTimedOut` with type `START_TO_CLOSE` | The worker started but never reported back (crash, sleep, kill) | Check the worker log, raise `timeout_s` |
| Activity completed with `exit_code` 124 | The script hit `timeout_s` and was killed | Raise `timeout_s` or speed up the script |
| Exit code 127 | Interpreter or script not found on the exec machine | Fix the path in the job definition |
| `WorkflowExecutionTimedOut` | The whole run exceeded `execution_timeout` | Usually a long offline exec machine |
