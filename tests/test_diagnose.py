"""History diagnosis over synthetic protobuf events (offline, no server)."""
from __future__ import annotations

from temporalio.api.common.v1 import Payloads
from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent
from temporalio.converter import default as default_converter

from legends_temporal import runtime, workflows

JOB = runtime.to_wire(runtime.code_job("canary-x", "canary", "exe", "python", "print(1)", timeout_s=60))


def _payloads(*values):
    return Payloads(payloads=default_converter().payload_converter.to_payloads(list(values)))


def _event(eid, secs, etype, attr, **kw):
    ev = HistoryEvent(event_id=eid, event_type=etype)
    ev.event_time.seconds = 1_800_000_000 + int(secs)
    ev.event_time.nanos = int((secs % 1) * 1e9)
    target = getattr(ev, attr)
    for k, v in kw.items():
        if isinstance(v, dict):
            sub = getattr(target, k)
            for sk, sv in v.items():
                setattr(sub, sk, sv)
        elif isinstance(v, Payloads):
            getattr(target, k).CopyFrom(v)
        else:
            setattr(target, k, v)
    return ev


def _started():
    return _event(1, 0, EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED, "workflow_execution_started_event_attributes",
                  workflow_type={"name": "ScriptJob"}, task_queue={"name": "ctl"}, input=_payloads(JOB), attempt=1)


def _activity(base, secs, name, queue, worker, result=None, failure=None):
    evs = [_event(base, secs, EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED, "activity_task_scheduled_event_attributes",
                  activity_type={"name": name}, task_queue={"name": queue}),
           _event(base + 1, secs, EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED, "activity_task_started_event_attributes",
                  scheduled_event_id=base, attempt=1, identity=worker)]
    if failure:
        ev = _event(base + 2, secs + 0.1, EventType.EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT,
                    "activity_task_timed_out_event_attributes", scheduled_event_id=base)
        ev.activity_task_timed_out_event_attributes.failure.message = failure
        evs.append(ev)
    else:
        evs.append(_event(base + 2, secs + 0.1, EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED,
                          "activity_task_completed_event_attributes", scheduled_event_id=base,
                          result=_payloads(result) if result is not None else Payloads()))
    return evs


def test_diagnose_completed_run():
    events = [_started(), *_activity(5, 3.0, "run_script", "exe", "w@EXEC",
                                     {"exit_code": 0, "duration_s": 0.1, "tail": "hello\nbattle-ok\n"}),
              _event(11, 3.2, EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED,
                     "workflow_execution_completed_event_attributes",
                     result=_payloads({"ok": True, "job": "canary-x", "exit_code": 0, "duration_s": 0.1}))]
    d = workflows.diagnose_events(events)
    v = d["verdict"]
    assert d["status"] == "COMPLETED" and v["ok"] is True
    assert v["script_exit_code"] == 0 and v["script_worker"] == "w@EXEC" and v["script_attempts"] == 1
    assert v["output_tail"] == ["hello", "battle-ok"]
    assert d["job"]["job_id"] == "canary-x" and d["job"]["exec_queue"] == "exe"
    assert d["workflow_type"] == "ScriptJob" and d["task_queue"] == "ctl"
    assert any("+3.0s #5 activity_task_scheduled: run_script -> exe" in t for t in d["timeline"])
    assert "failed_activity" not in v


def test_diagnose_failed_script_with_log_and_notice():
    events = [_started(),
              *_activity(5, 0, "run_script", "exe", "w@EXEC", {"exit_code": 3, "duration_s": 0.1, "tail": "boom"}),
              *_activity(11, 0.2, "log_failure", "ctl", "w@CTL", None),
              *_activity(17, 0.3, "notify_buzz", "exe", "w@EXEC", "buzz:lab")]
    fail = _event(23, 0.6, EventType.EVENT_TYPE_WORKFLOW_EXECUTION_FAILED, "workflow_execution_failed_event_attributes")
    fail.workflow_execution_failed_event_attributes.failure.message = "canary: exit code 3"
    d = workflows.diagnose_events(events + [fail])
    v = d["verdict"]
    assert d["status"] == "FAILED" and v["ok"] is False
    assert v["failed_activity"] == "run_script" and v["error"] == "script exited 3"
    assert v["failure_logged"] is True and v["notice"] == "posted"
    assert v["workflow_failure"] == "canary: exit code 3"
    assert v["output_tail"] == ["boom"]


def test_diagnose_waiting_for_worker_hint():
    events = [_started(), _event(5, 1, EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED,
                                 "activity_task_scheduled_event_attributes",
                                 activity_type={"name": "run_script"}, task_queue={"name": "exe"})]
    v = workflows.diagnose_events(events)["verdict"]
    assert v["status"] == "RUNNING" and "queue describe" in v["hint"]


def test_diagnose_timed_out_activity():
    events = [_started(), *_activity(5, 0, "run_script", "exe", "w@EXEC", failure="activity ScheduleToStart timeout")]
    v = workflows.diagnose_events(events)["verdict"]
    assert v["failed_activity"] == "run_script" and "ScheduleToStart" in v["error"]


def test_diagnose_terminated_and_tail_limit():
    events = [_started(), *_activity(5, 0, "run_script", "exe", "w", {"exit_code": 0, "duration_s": 1,
                                                                      "tail": "\n".join(f"l{i}" for i in range(50))})]
    term = _event(9, 2, EventType.EVENT_TYPE_WORKFLOW_EXECUTION_TERMINATED,
                  "workflow_execution_terminated_event_attributes", reason="operator")
    d = workflows.diagnose_events(events + [term], tail_lines=5)
    assert d["status"] == "TERMINATED" and d["verdict"]["workflow_failure"] == "terminated: operator"
    assert d["verdict"]["output_tail"] == ["l45", "l46", "l47", "l48", "l49"]
