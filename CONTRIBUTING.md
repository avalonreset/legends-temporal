# Contributing

Start with [AGENTS.md](AGENTS.md), [docs/AGENT-RECIPE.md](docs/AGENT-RECIPE.md), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), and [docs/CLI-CONTRACT.json](docs/CLI-CONTRACT.json). Keep changes focused on reliable Temporal cluster operations, replay-safe `ScriptJob` execution, and clear failure diagnosis.

Use Python 3.11+ and a local checkout:

```sh
python -m pip install -e .
python -m pytest -q
python scripts/check_release.py
legends-temporal doctor
```

Run offline tests first; the test suite uses synthetic protobuf events and discard-port isolation so it never touches a live Temporal server. For end-to-end verification against a local or self-hosted server, follow [docs/LIVE-CANARY.md](docs/LIVE-CANARY.md) and keep private operational receipts outside this repository.

When adding or changing CLI flags or MCP tools, update [docs/CLI-CONTRACT.json](docs/CLI-CONTRACT.json) in the same commit: `tests/test_cli_contract.py` and `tests/test_mcp_tools.py` enforce exact parity between the argparse tree, MCP tool schemas, and the contract file. Keep the 11-field `ScriptJob` wire contract (`legends_temporal.runtime.WIRE_FIELDS`) stable so deployed workers remain backward- and forward-compatible.

Sanitize all examples, tests, and logs. See [SECURITY.md](SECURITY.md) for vulnerability reports.
