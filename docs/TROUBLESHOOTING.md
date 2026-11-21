# Troubleshooting

Start with `legends-temporal doctor --live --json`. Each failing check names the
layer.

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Exit 5, `CONNECT_TIMEOUT` | Wrong address, server down, firewall, overlay network down | `doctor --live`; check the address in `config show`; test the port; on a tailnet check the node is up |
| Exit 5, `CONNECT_FAILED` with TLS text | TLS mismatch: TLS on a plain server or the reverse, wrong CA or server name | Set or clear `tls`; set `tls_ca` and `tls_server_name`; an API key forces TLS |
| `namespace` FAIL, NOT_FOUND | Namespace not registered | Create it on the server (`temporal operator namespace create`) or fix `namespace` |
| `queue:<q>` FAIL, 0 live pollers | No worker on that queue, or the worker uses a different namespace | Start `legends-temporal worker`; compare the worker's profile; `queue describe <q>` |
| Timer stays `RUNNING` with only a start event | Delay not reached, or no control worker | `workflow describe <id>` shows the start delay; check `queue describe <workflow_queue>` |
| Activity `SCHEDULE_TO_START` timeout | Exec machine offline longer than `wait_for_worker_s` | Bring the worker back; raise `wait_for_worker_s` for laptops |
| Exit code 124 in the verdict | Script hit `timeout_s` | Raise `timeout_s` or fix the script |
| Exit code 127 | Interpreter or script path wrong on the exec machine | Use an absolute interpreter path in the profile or the job |
| `unknown key` error on apply | Typo or unsupported field in a definition | Fields are the 11 wire fields plus schedule keys listed in `schedules.SCHEDULE_KEYS` |
| `schedule create` says it exists | Ids are unique | Use `schedule apply` to update in place |
| `ui_http` WARN, connection refused | Hostname resolves to an address the UI does not listen on | Set `ui_resolve` to the reachable IP; the TLS name stays the URL host |
| `ssh_systemd` WARN | SSH not configured, BatchMode needs a key, or unit names differ | Configure keys; set `ssh.ssh_bin` if the default `ssh` is broken; fix `ssh.units` |
| Worker rejects the payload | A producer sent extra fields | Always build jobs with `runtime.to_wire` or this CLI |
| Notice not posted | Notifier misconfigured or channel empty | `notify.kind`, env var names, `channels` map; check the worker log |
| Arguments starting with `-` rejected | `--args` stops at option-like values | Use `--arg=--flag --arg=value` |
| `workflow history --diagnose` exit 0 on a failed run | By design: the diagnosis succeeded | Read `verdict.ok` and `verdict.status` |

## Gathering evidence

```bash
legends-temporal workflow list --status failed --limit 10 --json
legends-temporal workflow history <id> --diagnose --json
legends-temporal workflow describe <id> --json
legends-temporal queue describe --json
```

## Windows notes

- Workers start scripts with `CREATE_NO_WINDOW`, so scheduled jobs do not open
  consoles. If a script needs a desktop session, run the exec worker in the
  user's interactive session, not as a service account.
- Some Windows OpenSSH builds exit 255 silently in non-interactive use. Point
  `ssh.ssh_bin` at another `ssh.exe` (for example Git for Windows) if the SSH
  check fails while manual SSH works.
