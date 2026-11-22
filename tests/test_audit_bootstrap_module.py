"""Unit tests for v0.1.1 features: cto-legends --module scheduling, audit, and bootstrap."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from legends_temporal import audit, bootstrap, cli, mcp_server, runtime, schedules, timers
from legends_temporal.config import Profile

PROFILE = Profile(name="t", address="127.0.0.1:9", workflow_queue="ctl", exec_queue="exe", timezone="UTC")


def test_module_job_compiles_to_11_field_wire_contract():
    job = runtime.module_job("mod-1", "Run Empire Digest", "exe", "python", "legends-empire", ["steward", "digest"])
    wire = runtime.to_wire(job)
    assert set(wire) == set(runtime.WIRE_FIELDS)
    assert wire["script"] == "-m"
    assert wire["args"] == ["cto_legends", "run", "legends-empire", "--", "steward", "digest"]
    with pytest.raises(ValueError):
        runtime.module_job("bad", "bad", "exe", "python", "not-a-legends-module")


def test_schedule_and_timer_dry_run_with_module(capsys):
    rc = cli.main([
        "schedule", "create", "--id", "weekly-geogrid", "--cron", "0 6 * * 1",
        "--module", "legends-geogrid", "--arg=report-readiness", "--dry-run", "--json"
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["schedules"][0]["command"] == ["python", "-m", "cto_legends", "run", "legends-geogrid", "--", "report-readiness"]

    rc2 = cli.main([
        "timer", "in", "15m", "--module", "legends-buzz", "--arg=doctor", "--arg=--json", "--dry-run", "--json"
    ])
    assert rc2 == 0
    out2 = json.loads(capsys.readouterr().out)
    assert out2["ok"] is True
    assert out2["job"]["script"] == "-m"
    assert out2["job"]["args"] == ["cto_legends", "run", "legends-buzz", "--", "doctor", "--json"]


def test_audit_definitions_catches_secrets_pileups_and_bad_crons(tmp_path):
    defs = [
        {
            "job_id": "clean-module-job",
            "kind": "module",
            "module": "legends-empire",
            "args": ["steward", "doctor"],
            "cron": "30 2 * * *",
            "timezone": "America/New_York",
        },
        {
            "job_id": "pileup-and-secret-job",
            "script": "notify.py",
            "args": ["https://hooks.slack.com/services/T00/B00/X00"],
            "interval": "5m",
            "timeout_s": 600,
            "overlap": "ALLOW_ALL",
        },
        {
            "job_id": "bad-cron-job",
            "script": "run.py",
            "cron": "30 2 * *",
            "timezone": "Mars/Phobos",
        },
    ]
    res = audit.audit_definitions(defs, PROFILE)
    assert res["ok"] is False
    codes = {i["code"] for i in res["issues"]}
    assert "RAW_WEBHOOK_URL" in codes
    assert "OVERLAP_PILEUP_RISK" in codes
    assert "INVALID_CRON" in codes
    assert "INVALID_TIMEZONE" in codes

    clean_file = tmp_path / "clean.json"
    clean_file.write_text(json.dumps({"jobs": [defs[0]]}), encoding="utf-8")
    assert cli.main(["audit", "--file", str(clean_file), "--offline", "--json"]) == 0


def test_bootstrap_local_compose_and_systemd(tmp_path):
    loc = bootstrap.plan_local(PROFILE, db_path=str(tmp_path / "dev.db"), start=False, dry_run=True)
    assert loc["ok"] is True and loc["persistent"] is True
    assert "--db-filename" in loc["command"]

    with pytest.raises(ValueError):
        bootstrap.plan_compose(PROFILE, bind_ip="0.0.0.0")

    comp_dir = tmp_path / "compose-out"
    comp = bootstrap.plan_compose(PROFILE, output_dir=str(comp_dir), bind_ip="127.0.0.1", write=True)
    assert comp["ok"] is True and comp["action"] == "written"
    assert (comp_dir / "docker-compose.yml").is_file()
    assert "127.0.0.1:7233:7233" in (comp_dir / "docker-compose.yml").read_text(encoding="utf-8")

    sysd_dir = tmp_path / "systemd-out"
    sysd = bootstrap.plan_systemd(PROFILE, output_dir=str(sysd_dir), role="control", write=True)
    assert sysd["ok"] is True and sysd["action"] == "written"
    assert (sysd_dir / "legends-temporal-worker-control.service").is_file()


def test_mcp_new_tools_and_module_timer(tmp_path):
    r1 = mcp_server.handle_message({
        "jsonrpc": "2.0", "id": 10, "method": "tools/call",
        "params": {"name": "temporal_timer_schedule", "arguments": {"delay": "5m", "module": "legends-empire", "args": ["steward", "doctor"]}}
    }, PROFILE)
    b1 = json.loads(r1["result"]["content"][0]["text"])
    assert b1["preview"] is True and b1["job"]["args"][:3] == ["cto_legends", "run", "legends-empire"]

    r2 = mcp_server.handle_message({
        "jsonrpc": "2.0", "id": 11, "method": "tools/call",
        "params": {"name": "temporal_bootstrap_plan", "arguments": {"target": "compose", "bind_ip": "127.0.0.1"}}
    }, PROFILE)
    b2 = json.loads(r2["result"]["content"][0]["text"])
    assert b2["ok"] is True and b2["target"] == "compose"

    clean_file = tmp_path / "clean.json"
    clean_file.write_text(json.dumps({"jobs": [{"job_id": "a", "script": "a.py", "cron": "0 1 * * *"}]}), encoding="utf-8")
    r3 = mcp_server.handle_message({
        "jsonrpc": "2.0", "id": 12, "method": "tools/call",
        "params": {"name": "temporal_schedule_audit", "arguments": {"file": str(clean_file), "offline": True}}
    }, PROFILE)
    b3 = json.loads(r3["result"]["content"][0]["text"])
    assert b3["ok"] is True and b3["checked_count"] == 1
