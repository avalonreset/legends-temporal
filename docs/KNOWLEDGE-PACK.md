# Knowledge pack: `knowledge/temporal`

Schema `legends.knowledge-pack/v1`, manifest `knowledge/temporal/pack.json`.
Read only the note that matches the task.

| Note | Read when |
| --- | --- |
| `01-temporal-mental-model-and-determinism.md` | Explaining namespaces, task queues, workflows, activities, workers, replay safety |
| `02-schedules-and-durable-timers.md` | Replacing cron, Task Scheduler or sleep-then-run; choosing schedule vs timer vs run-now |
| `03-zero-rewrite-scriptjob-and-agentjob-pattern.md` | Wrapping scripts, commands, webhooks or agent turns; the 11-field contract |
| `04-timeouts-retries-and-idempotency.md` | Picking timeouts, reading timeout events, making jobs safe to rerun |
| `05-self-hosted-postgres-and-tailnet-hardening.md` | Running or securing a self-hosted server, keeping secrets out of history |
| `06-visibility-queries-and-failure-diagnosis.md` | Finding failed runs, reading `--diagnose`, checking pollers |

`legends-temporal resources --json` prints absolute paths for every note, the
recipe, the contract and the skill, so agents can open them without guessing
the install location. `doctor` reports WARN on the `recipe_and_knowledge_pack` check if
any of them is missing or the manifest schema is wrong.

The notes describe Temporal behaviour as of server 1.2x and Python SDK 1.x and
this module's defaults. They are not a substitute for the official docs at
<https://docs.temporal.io> for features this module does not use.
