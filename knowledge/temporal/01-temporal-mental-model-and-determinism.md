# 01. Temporal mental model and determinism

Temporal is a durable execution engine. You write ordinary code and the server makes it survive crashes, restarts and network failures by recording every decision in an event history.

## The pieces

- **Frontend service (gRPC, default port 7233).** Every client and worker talks to it. `legends-temporal` connects here.
- **Namespace.** An isolation unit with its own retention period, schedules, workflows and search attributes. Pick one namespace per environment or per house, not per job.
- **Task queue.** A named mailbox. Workers poll it; the server dispatches workflow tasks and activity tasks to whichever live poller picks them up. Queues are created on first use, so a typo silently creates an empty queue. `legends-temporal queue describe <q>` shows whether anything is actually polling.
- **Workflow.** Deterministic orchestration code. It decides what to do next and waits. Workflow code runs on a worker, but its state lives in the server history.
- **Activity.** Ordinary side-effecting code (run a script, call an API, post a message). Activities can run on any queue, so one workflow can drive work on several machines.
- **Worker.** A process that polls one task queue and hosts workflow and activity implementations.

## Determinism

When a worker restarts, Temporal replays the history through the workflow code to rebuild state. Replay only works if the code makes the same decisions in the same order every time. Inside workflow code:

- Do not read the wall clock, random numbers, environment variables or files. Use `workflow.now()`, `workflow.random()` and activity results instead.
- Do not do network or disk I/O. Put it in an activity.
- Do not iterate over unordered collections when the order changes a decision.
- Changing workflow logic for already running executions needs `workflow.patched()` or a new workflow type. Activities can change freely.

The Python SDK runs workflow code in a sandbox that blocks most non-deterministic imports. Modules that are known to be safe (for example this package's dataclasses) are passed through with `SandboxRestrictions.with_passthrough_modules`.

## Why this module keeps workflows tiny

`legends-temporal` ships exactly one workflow type, `ScriptJob`, whose logic is: run an activity, and on failure log and notify, then fail. Every real behaviour lives in the script or agent message the activity runs. That keeps the workflow replay-safe forever: you change jobs by changing data, never by changing workflow code. See note 03.

## Event history in one paragraph

Each execution has an append-only list of events: `WorkflowExecutionStarted`, `WorkflowTaskScheduled/Started/Completed`, `ActivityTaskScheduled/Started/Completed/Failed/TimedOut`, timers, signals and a terminal event (`Completed`, `Failed`, `TimedOut`, `Canceled`, `Terminated`, `ContinuedAsNew`). `legends-temporal workflow history <id> --diagnose` reads that list and turns it into a verdict. See note 06.
