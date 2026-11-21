# Third-Party Notices

`legends-temporal` declares one runtime dependency in `pyproject.toml`:

- **`temporalio` (`>=1.8`, MIT License):** Official Temporal Python SDK (`https://github.com/temporalio/sdk-python`), used for gRPC client connections, schedule and workflow management, protobuf event history decoding, and hosting the `ScriptJob` worker.
- **`PyYAML` (`>=6`, MIT License, optional `[yaml]` extra):** Used only when loading `.yaml` / `.yml` schedule definition files via `legends-temporal schedule apply --file`. JSON and Python schedule inventories use the Python standard library only.
- **CTO Legends Central Router (`skills/cto-legends/SKILL.md`, MIT License):** Vendored byte-for-byte from `avalonreset/cto-legends`.
