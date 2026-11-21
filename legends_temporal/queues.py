# SPDX-License-Identifier: MIT
"""Task queue inspection: pollers for workflow and activity task queues."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .client import iso

STALE_POLLER_S = 120  # a live poller re-polls at least every ~70s


def _poller(p: Any, now: datetime) -> dict[str, Any]:
    last = iso(p.last_access_time)
    age = None
    if p.last_access_time and p.last_access_time.seconds:
        age = round(now.timestamp() - (p.last_access_time.seconds + p.last_access_time.nanos / 1e9), 1)
    return {"identity": p.identity, "last_access_time": last, "age_s": age,
            "rate_per_second": round(p.rate_per_second, 3), "live": age is not None and age <= STALE_POLLER_S}


async def describe_queue(client: Any, namespace: str, task_queue: str) -> dict[str, Any]:
    from temporalio.api.enums.v1 import TaskQueueType
    from temporalio.api.taskqueue.v1 import TaskQueue
    from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest

    now = datetime.now(timezone.utc)
    out: dict[str, Any] = {"ok": True, "task_queue": task_queue, "namespace": namespace}
    total_live = 0
    for label, tq_type in (("workflow", TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW),
                           ("activity", TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY)):
        resp = await client.workflow_service.describe_task_queue(DescribeTaskQueueRequest(
            namespace=namespace, task_queue=TaskQueue(name=task_queue), task_queue_type=tq_type))
        pollers = [_poller(p, now) for p in resp.pollers]
        total_live += sum(1 for p in pollers if p["live"])
        out[f"{label}_pollers"] = pollers
    out["live_pollers"] = total_live
    out["healthy"] = total_live > 0
    return out
