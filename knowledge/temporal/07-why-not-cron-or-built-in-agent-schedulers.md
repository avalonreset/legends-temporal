# 07 · Why Not Host Crontabs, Sleep Loops, or Built-In Chat App Schedulers?

Every week, developers and operators try to automate recurring research, SEO tracking, database backups, or delayed follow-up checks by reaching for whatever timer sits closest at hand: a `nohup bash -c "sleep 3600 && ..."` loop, a workstation `crontab` / Windows Scheduled Task, or the built-in "Automations / Scheduled Prompts" tab inside a desktop AI chat harness.

Within 72 hours, almost all of those setups silently fail. This note explains the structural failure modes of ad-hoc and built-in chat schedulers, and how `legends-temporal` turns Temporal into a durable, multi-machine execution clock for the entire `cto-legends` ecosystem.

---

## 1. The Five Failure Modes of Built-In Agent Schedulers and Crontabs

### Failure Mode 1: Interactive Approval Popups and 1-Hour Rate Walls
Desktop AI chat apps (Claude Desktop, Codex app harnesses, and IDE background tasks) treat scheduled prompts as interactive UI turns:
- **Minimum interval walls:** Many built-in app schedulers enforce a 1-hour minimum cadence or arbitrary daily run caps. You cannot schedule a 5-minute health check (`ops-geogrid-health`) or a 90-second post-deploy verification timer.
- **Unattended modal blocks:** When a scheduled turn tries to run a shell command or inspect an external endpoint, the desktop harness frequently pauses behind a human UI confirmation dialog. At 3:00 AM, nobody clicks "Allow", so the job stalls until morning.
- **Single-vendor lock-in:** A schedule saved inside one chat app's local SQLite database cannot dispatch work to a different subscription harness, cannot route dynamically by token quota (`model="auto"`), and vanishes if you switch agent hosts.

### Failure Mode 2: Laptop Sleep and Single-Machine Coupling
A host `crontab` or Windows Task Scheduler ties the **clock** and the **execution environment** to the same physical machine:
- If your workstation is asleep at 02:30 when a nightly GPU or local-vault job is due, `cron` simply skips the tick.
- `legends-temporal` decouples the clock from the workstation by splitting the **control queue** (`workflow_queue`, running on an always-on Linux VPS or local server) from the **exec queue** (`exec_queue`, running on the workstation or GPU node that owns the files). The workflow starts on the exact cron second and holds the `run_script` activity in the exec queue for up to `wait_for_worker_s` (6 hours by default). The moment your workstation wakes up and its worker polls the queue, the job executes.

### Failure Mode 3: Background `sleep` Processes Die with the Parent Shell
When a coding agent is asked to "check this deployment in 20 minutes", its default instinct is to spawn `sleep 1200 && python check.py &`.
- As soon as the agent's subshell exits, times out, or restarts, the background process receives `SIGHUP` / job-object termination and dies silently.
- `legends-temporal timer in 20m` delegates the wait to the Temporal server via `start_delay`. Zero worker process or shell needs to stay alive during the 20-minute countdown, and the timer survives worker restarts and server reboots.

### Failure Mode 4: Silent Cron Failures vs. Single-Screen Protobuf Diagnosis
When a cron job exits non-zero at night, the error output either vanishes into `/var/mail` or requires SSHing into a box and grepping log files. Conversely, querying raw Temporal workflow histories dumps 50 to 200 protobuf `HistoryEvent` objects (15,000+ tokens) into an agent's context window.
- `legends-temporal workflow history <id> --diagnose` reduces the entire protobuf event stream into a single compact JSON verdict: terminal status, failed activity, exact `script_exit_code` (`124` for timeout, `127` for missing binary), worker identity, bounded output tail, notice delivery status, relative-second timeline, and remediation hint.
- `legends-temporal audit` scans the entire cluster in one command to catch **orphaned task queues** (a schedule targeting an `exec_queue` with `0` live activity pollers), paused schedules, missed catchup windows, overlap pile-up hazards, and 3-way drift between your git-tracked schedule inventory and the live server.

### Failure Mode 5: Plaintext Secret Leakage in Workflow History
When developers write custom Temporal workflows or ad-hoc scheduler payloads, they frequently pass raw webhook URLs, API keys, or bearer tokens in the job arguments. Temporal persists every workflow input in PostgreSQL forever and displays it in the Web UI.
- `legends-temporal` enforces reference-only secret hygiene (`--webhook-env`, `api_key_env`, and local notifier identity files) and `legends-temporal audit` actively scans schedule arguments for leaked webhook URLs or token strings before they ever touch a cluster.

---

## 2. First-Class `cto-legends` Ecosystem Scheduling (`--module`)

Because every tool in the `cto-legends` stack is an isolated CLI module with deterministic JSON output, `legends-temporal` can schedule any installed module directly via `--module <legends-module>` (or `"kind": "module", "module": "legends-..."` in declarative JSON/YAML inventories) while preserving the frozen 11-field `ScriptJob` wire contract:

```bash
# Preview a weekly Monday 06:00 DataForSEO or GeoGrid check
legends-temporal schedule create \
  --id weekly-geogrid-readiness \
  --cron "0 6 * * 1" \
  --module legends-geogrid \
  --arg=report-readiness \
  --dry-run

# Schedule a daily 07:00 Empire vault health digest
legends-temporal schedule create \
  --id daily-empire-digest \
  --cron "0 7 * * *" \
  --module legends-empire \
  --arg=steward --arg=digest \
  --dry-run

# Fire a one-shot delayed timer in 15 minutes to run a Buzz relay probe
legends-temporal timer in 15m \
  --module legends-buzz \
  --arg=doctor --arg=--json \
  --dry-run
```

Under the hood, `module_job` compiles the request into the standard 11-field `ScriptJob` payload (`interpreter`, `script="-m"`, `args=["cto_legends", "run", "<module>", "--", ...]`), so existing `ScriptJob` workers execute any `cto-legends` module without custom workflow definitions or worker restarts.
