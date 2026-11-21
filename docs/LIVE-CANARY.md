# Live canary

Offline tests use fakes. This procedure is the live proof that a profile,
server and workers work end to end. All steps are read-only except step 5,
which starts one harmless inline-Python job.

## Procedure

```bash
# 1. readiness: connect, namespace, pollers, optional UI and SSH units
legends-temporal doctor --live --json

# 2. inventory
legends-temporal namespace list
legends-temporal queue describe <workflow_queue> <exec_queue>
legends-temporal schedule list
legends-temporal schedule describe <schedule-id>

# 3. recent executions
legends-temporal workflow list --limit 5

# 4. diagnose one completed and one failed run
legends-temporal workflow history <completed-id> --diagnose
legends-temporal workflow history <failed-id> --diagnose

# 5. durable timer round trip
legends-temporal timer in 3s --code "print('legends-temporal-battle-test-ok')" --notify-channel <channel> --wait
legends-temporal workflow history <timer-workflow-id> --diagnose
```

## Pass criteria

- Step 1 prints `ok: true` with PASS on `grpc_connect`, `namespace` and every
  `queue:*` check.
- Step 4 shows `script_exit_code` 0 for the completed run, and for the failed
  run the failed activity, a non-zero exit code, `failure_logged: true` and the
  notice state.
- Step 5 returns `status: COMPLETED`, `exit_code: 0`, and the diagnosis shows
  `run_script` scheduled about 3 seconds after the start event, with the output
  tail `legends-temporal-battle-test-ok`.

## Forcing a failure (optional)

```bash
legends-temporal timer in 3s --code "import sys; print('forced'); sys.exit(3)" --notify-channel <test-channel> --wait
```

Expect exit 4, a `FAILED` diagnosis with `script_exit_code: 3`, and one notice
in the test channel. Use a test channel, not a production one.

## Recorded runs

Live results belong in the operator's own receipts, not in this repository.
Record the module version, server version, date, the workflow ids and the pass
criteria above.
