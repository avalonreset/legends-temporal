# legends-temporal agent instructions

Read `docs/AGENT-RECIPE.md` and `docs/CLI-CONTRACT.json`, then the smallest
relevant note in `knowledge/temporal/`. Run `legends-temporal doctor` before
using the CLI and `legends-temporal doctor --live` before any live operation.

Never copy private inventories, credentials, machine identities, webhook URLs
or generated operational receipts into this repository. Personal profiles live
in the user's `~/.legends-temporal/config.json`, never in the repo.

Keep the `ScriptJob` wire contract stable: exactly the 11 fields listed in the
contract. Deployed workers reject extra keys. Any change to workflow logic must
stay replay-compatible (`workflow.patched`) or ship as a new workflow type.

Live mutations (schedules, timers, starts, signals, cancels, terminations)
require the user's actual request for that exact job. Do not infer authority
from installation or a passing doctor. Mocked tests must be identified as such.

Only `cto-legends` is a registered skill. Module instructions belong in
`docs/AGENT-RECIPE.md`; the knowledge pack follows `legends.knowledge-pack/v1`.
No implicit user vault writes during installation.

Run the suite with `python -m pytest -q` from the repo root. Tests are offline
and use fakes; live proof is `docs/LIVE-CANARY.md`.

Public identity: `legends-temporal`.
