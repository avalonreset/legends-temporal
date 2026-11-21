# Release Verification (`legends-temporal`)

Run all offline release, contract, diagnosis, MCP, and portability checks from the repository root:

```bash
python scripts/render_cards.py
python scripts/check_release.py
python -m pytest -q
legends-temporal version --json
legends-temporal doctor --json
legends-temporal resources --json
legends-temporal config show --json
```

To build distribution artifacts and verify live cluster operation against a dev or self-hosted Temporal server, follow [docs/LIVE-CANARY.md](LIVE-CANARY.md):

```bash
python -m build
legends-temporal doctor --live --json
```
