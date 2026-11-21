# SPDX-License-Identifier: MIT
"""Namespace and cluster inspection (read-only)."""
from __future__ import annotations

from typing import Any

from .client import seconds


def _ns_dict(resp: Any) -> dict[str, Any]:
    from temporalio.api.enums.v1 import NamespaceState
    info, cfg = resp.namespace_info, resp.config
    return {"name": info.name, "state": NamespaceState.Name(info.state).replace("NAMESPACE_STATE_", ""),
            "description": info.description or None, "owner_email_present": bool(info.owner_email),
            "id": info.id, "retention_days": round((seconds(cfg.workflow_execution_retention_ttl) or 0) / 86400, 2),
            "history_archival": cfg.history_archival_state, "visibility_archival": cfg.visibility_archival_state,
            "is_global": resp.is_global_namespace}


async def list_namespaces(client: Any) -> dict[str, Any]:
    from temporalio.api.workflowservice.v1 import ListNamespacesRequest
    items: list[dict[str, Any]] = []
    token = b""
    while True:
        resp = await client.workflow_service.list_namespaces(
            ListNamespacesRequest(page_size=100, next_page_token=token))
        items.extend(_ns_dict(ns) for ns in resp.namespaces)
        token = resp.next_page_token
        if not token:
            break
    items.sort(key=lambda n: n["name"])
    return {"ok": True, "count": len(items), "namespaces": items}


async def describe_namespace(client: Any, name: str) -> dict[str, Any]:
    from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
    resp = await client.workflow_service.describe_namespace(DescribeNamespaceRequest(namespace=name))
    out = {"ok": True, **_ns_dict(resp)}
    out.update(await cluster_info(client))
    return out


async def cluster_info(client: Any) -> dict[str, Any]:
    from temporalio.api.workflowservice.v1 import GetClusterInfoRequest, GetSystemInfoRequest
    out: dict[str, Any] = {}
    try:
        ci = await client.workflow_service.get_cluster_info(GetClusterInfoRequest())
        out["cluster"] = {"name": ci.cluster_name, "server_version": ci.server_version,
                          "history_shard_count": ci.history_shard_count,
                          "persistence_store": ci.persistence_store or None,
                          "visibility_store": ci.visibility_store or None}
    except Exception as exc:  # noqa: BLE001 - some hosted clusters hide this
        out["cluster"] = {"error": str(exc)[:200]}
    try:
        si = await client.workflow_service.get_system_info(GetSystemInfoRequest())
        out["server_version"] = si.server_version
    except Exception:  # noqa: BLE001
        pass
    return out
