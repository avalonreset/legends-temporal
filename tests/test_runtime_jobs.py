"""ScriptJob wire contract, job builders, executor, notifier and timeouts (offline)."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import fields

import pytest

from legends_temporal import runtime
from legends_temporal.config import NotifyConfig

WIRE = ["job_id", "title", "queue", "interpreter", "script", "args", "cwd", "timeout_s",
        "wait_for_worker_s", "notify_channel", "cron"]


def test_wire_fields_are_exactly_the_house_contract():
    assert list(runtime.WIRE_FIELDS) == WIRE
    assert [f.name for f in fields(runtime.ScriptJob)] == WIRE


def test_to_wire_drops_extras_and_normalizes():
    w = runtime.to_wire({"job_id": "a", "title": "A", "queue": "q", "interpreter": "py", "script": "s.py",
                         "args": [1, "x"], "timeout_s": "30", "kind": "code", "remaining_actions": 2, "cwd": None})
    assert list(w) == WIRE
    assert w["args"] == ["1", "x"] and w["timeout_s"] == 30 and w["cwd"] == "" and w["cron"] == ""


def test_to_wire_from_object_with_private_attrs():
    job = runtime.script_job("a", "A", "q", "py", "s.py")
    job._remaining_actions = 4  # house inventories carry this outside the wire set
    assert list(runtime.to_wire(job)) == WIRE
    assert runtime.remaining_actions_of(job) == 4
    assert runtime.remaining_actions_of({"remaining_actions": 2}) == 2


def test_to_wire_requires_core_fields():
    with pytest.raises(ValueError):
        runtime.to_wire({"job_id": "a", "title": "A", "queue": "q"})


def test_code_job():
    j = runtime.code_job("c", "C", "q", "python", "print(1)", ["x"])
    assert (j.script, j.args) == ("-c", ["print(1)", "x"])


def test_agent_job_args_and_validation():
    j = runtime.agent_job("ag", "Ag", "q", "python", "/opt/ctl.py", "sid", "do it", model="m")
    assert j.script == "/opt/ctl.py"
    assert j.args == ["send", "sid", "--message", "do it", "--model", "m"]
    assert j.timeout_s == 120
    with pytest.raises(ValueError):
        runtime.agent_job("ag", "Ag", "q", "python", "", "sid", "m")
    with pytest.raises(ValueError):
        runtime.agent_job("ag", "Ag", "q", "python", "/c.py", "sid", "")


def test_command_job_runs_argv_and_propagates_exit():
    j = runtime.command_job("cmd", "Cmd", "q", sys.executable,
                            [sys.executable, "-c", "import sys; print('argv-ok'); sys.exit(7)"], timeout_s=60)
    r = runtime.execute_script(j)
    assert r.exit_code == 7
    with pytest.raises(ValueError):
        runtime.command_job("cmd", "Cmd", "q", "python", [])


def test_webhook_job_keeps_url_out_of_payload(monkeypatch):
    j = runtime.webhook_job("wh", "Wh", "q", sys.executable, "LT_TEST_HOOK", {"ref": "main"}, timeout_s=30)
    blob = json.dumps(runtime.to_wire(j))
    assert "LT_TEST_HOOK" in blob and "http" not in blob
    monkeypatch.delenv("LT_TEST_HOOK", raising=False)
    r = runtime.execute_script(j)
    assert r.exit_code != 0 and "LT_TEST_HOOK" in r.tail


def test_execute_script_success_and_tail():
    j = runtime.code_job("ok", "Ok", "q", sys.executable, "print('x' * 5000); print('END')", timeout_s=60)
    r = runtime.execute_script(j)
    assert r.exit_code == 0 and r.tail.rstrip().endswith("END") and len(r.tail) <= runtime.TAIL_CHARS


def test_execute_script_timeout_is_124():
    j = runtime.code_job("slow", "Slow", "q", sys.executable, "import time; time.sleep(10)", timeout_s=1)
    r = runtime.execute_script(j)
    assert r.exit_code == 124 and "TIMEOUT" in r.tail


def test_execute_script_missing_interpreter_is_127():
    j = runtime.script_job("nf", "Nf", "q", "definitely-not-an-interpreter-xyz", "a.py")
    assert runtime.execute_script(j).exit_code == 127


def test_no_window_flag():
    expected = 0x08000000 if os.name == "nt" else 0
    assert runtime.NO_WINDOW == expected == runtime.subprocess_flags()


def test_execution_timeouts():
    job = runtime.to_wire(runtime.script_job("a", "A", "q", "py", "s", timeout_s=100, wait_for_worker_s=50))
    assert runtime.schedule_execution_timeout(job).total_seconds() == 150 + 3 * 3600
    assert runtime.timer_execution_timeout(job, 30).total_seconds() == 180 + 3600


def test_failure_notice_text():
    t = runtime.failure_notice_text("nightly", "exe", "exit code 3", "wf-1", "line1\nboom")
    assert "nightly failed on exe (exit code 3)" in t and "Run: wf-1" in t and "boom" in t
    assert "did not run" in runtime.failure_notice_text("n", "q", "no worker", "wf", "")
    long = runtime.failure_notice_text("n", "q", "x", "wf", "y" * 10000)
    assert len(long) < 10000


def test_notifier_stdout_and_none(capsys):
    n = runtime.Notice(channel="ops", text="hello\nworld")
    assert runtime.send_notice(n, NotifyConfig(kind="stdout")) == "stdout"
    assert "LEGENDS_TEMPORAL_NOTICE [ops] hello | world" in capsys.readouterr().out
    assert runtime.send_notice(n, NotifyConfig(kind="none")) == "suppressed"


def test_notifier_webhook_requires_env(monkeypatch):
    monkeypatch.delenv("LT_NOTIFY_HOOK", raising=False)
    with pytest.raises(RuntimeError):
        runtime.send_notice(runtime.Notice("ops", "x"), NotifyConfig(kind="webhook", webhook_url_env="LT_NOTIFY_HOOK"))


def test_notifier_webhook_posts_json(monkeypatch):
    sent = {}

    class Resp:
        status = 204
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        sent["url"], sent["body"] = req.full_url, json.loads(req.data)
        return Resp()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("LT_NOTIFY_HOOK", "https://example.invalid/hook")
    out = runtime.send_notice(runtime.Notice("ops", "x"), NotifyConfig(kind="webhook", webhook_url_env="LT_NOTIFY_HOOK"))
    assert out == "webhook:204" and sent["body"] == {"channel": "ops", "text": "x"}


def test_notifier_unknown_kind():
    with pytest.raises(RuntimeError):
        runtime.send_notice(runtime.Notice("ops", "x"), NotifyConfig(kind="carrier-pigeon"))


def test_workflow_and_activity_names():
    from temporalio import activity, workflow
    assert workflow._Definition.from_class(runtime.ScriptJobWorkflow).name == "ScriptJob"
    names = {activity._Definition.from_callable(f).name for f in (runtime.run_script, runtime.notify_buzz,
                                                                   runtime.log_failure)}
    assert names == {"run_script", "notify_buzz", "log_failure"}


def test_worker_identity_mentions_role():
    assert "exec" in runtime.worker_identity("exec")
