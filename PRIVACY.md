# Privacy

`legends-temporal` is local-first and includes no telemetry, analytics, or background phone-home activity.

## Offline by default

- `legends-temporal doctor` (without `--live`), `legends-temporal resources`, `legends-temporal config show`, `legends-temporal job list`, and `--dry-run` previews (`schedule create --dry-run`, `schedule apply --dry-run`, `timer in --dry-run`) run completely offline.
- Packaged documentation (`docs/`) and Temporal operational notes (`knowledge/temporal/`) are resolved from the local installation without network requests.

## Explicit Temporal frontend egress

- Live commands (`doctor --live`, `namespace`, `queue`, `schedule`, `timer`, `workflow`, `job run`, `worker`) connect exclusively over gRPC to the Temporal address configured by `--address`, `TEMPORAL_ADDRESS`, or the active profile in `~/.legends-temporal/config.json`.
- API keys (`TEMPORAL_API_KEY` or the profile's `api_key_env`), webhook URLs (`webhook_env`), and notification credentials are read from environment variables or local files on the worker at runtime and are never embedded in Temporal workflow payloads or printed by `config show`.

See [SECURITY.md](SECURITY.md) for vulnerability reporting and credential hygiene rules.
