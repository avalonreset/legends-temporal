# Security

Bind self-hosted Temporal frontends (`7233`) and Web UIs to `127.0.0.1` or a private overlay network (Tailscale, WireGuard, or an internal VPC), or require mTLS (`tls_cert`, `tls_key`, `tls_ca`) or Temporal Cloud API keys (`TEMPORAL_API_KEY`). Never expose an unauthenticated Temporal frontend on `0.0.0.0`.

## Secrets and workflow history

Temporal records workflow inputs, activity payloads, and results in its database. Never pass API tokens, database passwords, private keys, or raw webhook URLs in job arguments (`--args`, `--arg`), inline `--code`, or `--body`. Instead:

- Load secrets from environment variables or files on the exec worker at runtime.
- Use `--webhook-env VAR_NAME` so the exec worker reads the webhook URL from `VAR_NAME` without storing the URL in Temporal history.
- Keep `api_key_env` pointed at an environment variable name; `legends-temporal config show` reports only `api_key_present: true|false`.

## Mutations and confirmations

Read commands (`doctor`, `namespace`, `queue`, `schedule list|describe`, `workflow list|describe|history`, `job list`, `config show`, `resources`) do not mutate cluster state. Mutating commands support `--dry-run` previews (`schedule create --dry-run`, `schedule apply --dry-run`, `timer in --dry-run`), and destructive actions (`schedule delete`, `workflow terminate`) require `--yes`. In the stdio MCP server (`legends-temporal mcp`), `temporal_timer_schedule` returns a dry-run preview unless `confirm: true` is explicitly passed.

## Reporting a vulnerability

Do not post credentials, private profile configs, or production workflow histories in public issues. Report security vulnerabilities privately through GitHub private vulnerability reporting on `avalonreset/legends-temporal` or contact the maintainer without attaching secrets.
