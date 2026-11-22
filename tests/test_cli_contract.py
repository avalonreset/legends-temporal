"""CLI contract, parsers, query and schedule builders, config resolution and dry runs (offline)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from legends_temporal import cli, schedules, timers, workflows
from legends_temporal.config import ConfigError, Profile, profile_from_dict, resolve_profile

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT / "docs/CLI-CONTRACT.json").read_text(encoding="utf-8"))
DEAD = "127.0.0.1:9"


def _parser_commands(p, prefix=""):
    out = {}
    for a in p._actions:
        if isinstance(a, argparse._SubParsersAction):
            for name, sp in a.choices.items():
                if any(isinstance(x, argparse._SubParsersAction) for x in sp._actions):
                    out.update(_parser_commands(sp, prefix + name + " "))
                    continue
                opts = []
                for x in sp._actions:
                    if x.dest == "help" or isinstance(x, argparse._SubParsersAction):
                        continue
                    if x.option_strings:
                        if x.default != argparse.SUPPRESS:
                            opts.append(x.option_strings[-1])
                    else:
                        opts.append(f"<{x.dest}>")
                out[(prefix + name).strip()] = opts
    return out


# ---------------- contract vs parser ----------------

def test_contract_matches_parser_exactly():
    actual = _parser_commands(cli.build_parser())
    assert set(actual) == set(CONTRACT["commands"])
    for cmd, opts in CONTRACT["commands"].items():
        assert sorted(actual[cmd]) == sorted(opts), cmd


def test_contract_metadata_is_consistent():
    cmds = set(CONTRACT["commands"])
    assert set(CONTRACT["mutating"]) <= cmds
    assert set(CONTRACT["dry_run_capable"]) <= cmds
    for cmd in CONTRACT["dry_run_capable"]:
        assert "--dry-run" in CONTRACT["commands"][cmd]
    for cmd, flag in CONTRACT["confirm_required"].items():
        assert flag in CONTRACT["commands"][cmd]
    assert CONTRACT["exit_codes"] == {"0": "ok", "1": "error", "2": "usage error", "3": "not found",
                                      "4": "workflow failed (start --wait, job run, timer --wait)",
                                      "5": "cannot connect to the Temporal frontend"}


def test_exit_code_constants_match_contract():
    from legends_temporal import client
    assert (client.EXIT_OK, client.EXIT_ERROR, client.EXIT_USAGE, client.EXIT_NOT_FOUND,
            client.EXIT_WORKFLOW_FAILED, client.EXIT_CONNECT) == (0, 1, 2, 3, 4, 5)


def test_global_flags_work_before_and_after_subcommand():
    p = cli.build_parser()
    a = p.parse_args(["--json", "--profile", "x", "schedule", "list"])
    b = p.parse_args(["schedule", "list", "--json", "--profile", "x"])
    assert a.json and b.json and a.profile == b.profile == "x"


def test_version_command(capsys):
    assert cli.main(["version", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["version"] == "0.1.2"


def test_usage_error_exit_code():
    with pytest.raises(SystemExit) as e:
        cli.main(["schedule"])
    assert e.value.code == 2


def test_resources_command(capsys):
    assert cli.main(["resources", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert "docs/AGENT-RECIPE.md" in out["paths"]


# ---------------- delay parser ----------------

@pytest.mark.parametrize("spec,secs", [("90", 90), ("90s", 90), ("15m", 900), ("2h", 7200), ("1d", 86400),
                                       ("1w", 604800), ("1h30m", 5400), ("1d2h3m4s", 93784), (" 10M ", 600)])
def test_parse_delay_valid(spec, secs):
    assert timers.parse_delay(spec) == secs


def test_parse_delay_zero_means_now():
    assert timers.parse_delay("0") == 0


@pytest.mark.parametrize("spec", ["", "abc", "10x", "-5m", "367d", "m5"])
def test_parse_delay_invalid(spec):
    with pytest.raises(ValueError):
        timers.parse_delay(spec)


# ---------------- visibility query builder ----------------

def test_build_query_combines_clauses():
    q = workflows.build_query(status="failed", wf_type="ScriptJob", id_prefix="canary-", query="StartTime > '2026'")
    assert q == ('ExecutionStatus="Failed" AND WorkflowType="ScriptJob" AND WorkflowId STARTS_WITH "canary-" '
                 "AND (StartTime > '2026')")


def test_build_query_empty_and_aliases():
    assert workflows.build_query() is None
    assert workflows.build_query(status="RUNNING") == 'ExecutionStatus="Running"'
    assert workflows.build_query(status="timed-out") == 'ExecutionStatus="TimedOut"'
    with pytest.raises(ValueError):
        workflows.build_query(status="bogus")


def test_build_query_escapes_quotes():
    assert workflows.build_query(id_prefix='a"b\\c') == 'WorkflowId STARTS_WITH "a\\"b\\\\c"'


# ---------------- schedule spec builder ----------------

def _profile(**kw):
    base = dict(name="t", address=DEAD, workflow_queue="ctl", exec_queue="exe", timezone="Europe/Berlin")
    base.update(kw)
    return Profile(**base)


def test_build_schedule_cron_defaults():
    from temporalio.client import ScheduleOverlapPolicy
    prof = _profile()
    d = schedules.expand_definition({"job_id": "nightly", "script": "run.py", "cron": "30 2 * * *"}, prof)
    wire, sched = schedules.split_definition(d)
    s = schedules.build_schedule(wire, sched, prof)
    assert s.spec.cron_expressions == ["30 2 * * *"]
    assert s.spec.time_zone_name == "Europe/Berlin"
    assert s.policy.overlap == ScheduleOverlapPolicy.SKIP
    assert s.policy.catchup_window.total_seconds() == 12 * 3600
    assert s.action.workflow == "ScriptJob"
    assert s.action.task_queue == "ctl"
    assert s.action.id == "nightly"
    assert s.action.execution_timeout.total_seconds() == 6 * 3600 + 900 + 3 * 3600
    assert wire["queue"] == "exe"
    assert not s.state.limited_actions


def test_build_schedule_interval_limited_paused():
    prof = _profile()
    d = schedules.expand_definition({"job_id": "hb", "kind": "code", "code": "print(1)", "interval": "15m",
                                     "jitter": "30s", "overlap": "buffer_one", "remaining_actions": 3,
                                     "paused": True, "note": "hold"}, prof)
    wire, sched = schedules.split_definition(d)
    s = schedules.build_schedule(wire, sched, prof)
    assert s.spec.intervals[0].every.total_seconds() == 900
    assert s.spec.jitter.total_seconds() == 30
    assert s.state.paused and s.state.note == "hold"
    assert s.state.limited_actions and s.state.remaining_actions == 3
    assert wire["script"] == "-c" and wire["args"] == ["print(1)"]


def test_build_schedule_rejects_missing_spec_and_bad_overlap():
    prof = _profile()
    wire, sched = schedules.split_definition(schedules.expand_definition({"job_id": "x", "script": "a"}, prof))
    with pytest.raises(ValueError):
        schedules.build_schedule(wire, sched, prof)
    wire, sched = schedules.split_definition(
        schedules.expand_definition({"job_id": "x", "script": "a", "cron": "* * * * *", "overlap": "nope"}, prof))
    with pytest.raises(ValueError):
        schedules.build_schedule(wire, sched, prof)


def test_split_definition_rejects_unknown_keys():
    with pytest.raises(ValueError):
        schedules.split_definition(schedules.expand_definition(
            {"job_id": "x", "script": "a", "cron": "* * * * *", "typo_field": 1}, _profile()))


def test_agent_definition_expands_to_send_args():
    prof = _profile(agent_ctl="/opt/ctl.py")
    d = schedules.expand_definition({"job_id": "rev", "kind": "agent", "cron": "0 9 * * 1",
                                     "session_id": "abc123", "message": "hi", "model": "m1"}, prof)
    wire, sched = schedules.split_definition(d)
    assert wire["script"] == "/opt/ctl.py"
    assert wire["args"] == ["send", "abc123", "--message", "hi", "--model", "m1"]
    assert wire["cron"] == "0 9 * * 1"  # regression: agent expansion must keep the cron
    assert schedules.build_schedule(wire, sched, prof).spec.cron_expressions == ["0 9 * * 1"]


def test_load_definitions_example_file():
    defs = schedules.load_definitions(str(ROOT / "examples/jobs.example.json"))
    assert [d["job_id"] for d in defs] == ["nightly-report", "heartbeat", "weekly-agent-review"]


def test_load_definitions_mapping_form(tmp_path):
    f = tmp_path / "defs.json"
    f.write_text(json.dumps({"jobs": {"a": {"script": "a.py", "cron": "* * * * *"}}}), encoding="utf-8")
    assert schedules.load_definitions(str(f))[0]["job_id"] == "a"


def test_load_python_jobs(tmp_path):
    pkg = tmp_path / "fakejobs"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "jobs.py").write_text(
        "from dataclasses import dataclass, field\n"
        "@dataclass\nclass J:\n job_id: str; title: str; queue: str; interpreter: str; script: str\n"
        " args: list = field(default_factory=list); cwd: str = ''; timeout_s: int = 60\n"
        " wait_for_worker_s: int = 60; notify_channel: str = 'ops'; cron: str = '5 * * * *'\n"
        "JOBS = {'j1': J('j1', 'one', 'q', 'python', 's.py')}\n", encoding="utf-8")
    defs = schedules.load_python_jobs("fakejobs.jobs:JOBS", [str(tmp_path)])
    assert defs[0]["job_id"] == "j1" and defs[0]["cron"] == "5 * * * *"


def test_calendar_to_cron_collapses_full_ranges():
    from temporalio.client import ScheduleCalendarSpec, ScheduleRange
    cal = ScheduleCalendarSpec(minute=[ScheduleRange(10)], hour=[ScheduleRange(0, 23)],
                               day_of_month=[ScheduleRange(1, 31)], month=[ScheduleRange(1, 12)],
                               day_of_week=[ScheduleRange(1)])
    assert schedules.calendar_to_cron(cal) == "10 * * * 1"


# ---------------- config resolution ----------------

def test_resolve_profile_portable_default(tmp_path):
    p = resolve_profile(env={"LEGENDS_TEMPORAL_CONFIG": str(tmp_path / "none.json")}, probe=lambda a: False)
    assert p.address == "localhost:7233" and p.namespace == "default"


def test_resolve_profile_order(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"default_profile": "a", "profiles": {
        "a": {"address": "a:7233", "namespace": "na"},
        "b": {"address": "b:7233", "namespace": "nb"}}}), encoding="utf-8")
    assert resolve_profile(config=str(cfg), env={}).address == "a:7233"
    assert resolve_profile(profile="b", config=str(cfg), env={}).namespace == "nb"
    env = {"TEMPORAL_ADDRESS": "env:7233", "TEMPORAL_NAMESPACE": "nenv"}
    p = resolve_profile(config=str(cfg), env=env)
    assert (p.address, p.namespace) == ("env:7233", "nenv")
    p = resolve_profile(config=str(cfg), address="flag:7233", namespace="nflag", env=env)
    assert (p.address, p.namespace) == ("flag:7233", "nflag")


def test_resolve_profile_detect(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"profiles": {"home": {"address": "h:7233", "detect": True}}}), encoding="utf-8")
    assert resolve_profile(config=str(cfg), env={}, probe=lambda a: True).address == "h:7233"
    assert resolve_profile(config=str(cfg), env={}, probe=lambda a: False).address == "localhost:7233"


def test_profile_rejects_unknown_keys_and_missing_profile(tmp_path):
    with pytest.raises(ConfigError):
        profile_from_dict("x", {"adress": "typo"}, "test")
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"profiles": {}}), encoding="utf-8")
    with pytest.raises(ConfigError):
        resolve_profile(profile="missing", config=str(cfg), env={})


def test_public_dict_hides_api_key(monkeypatch):
    monkeypatch.setenv("MY_KEY", "s3cret-value")
    p = Profile(name="c", api_key_env="MY_KEY")
    d = p.public_dict()
    assert d["api_key_present"] is True
    assert "s3cret-value" not in json.dumps(d)


def test_config_init_and_show(tmp_path, capsys):
    path = tmp_path / "cfg.json"
    assert cli.main(["config", "init", "--path", str(path), "--json"]) == 0
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["default_profile"] == "local"
    capsys.readouterr()
    assert cli.main(["config", "init", "--path", str(path), "--json"]) != 0  # refuses overwrite
    capsys.readouterr()
    assert cli.main(["config", "show", "--config", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["profile"]["address"] == "localhost:7233"


# ---------------- dry runs never touch the network ----------------

def test_timer_dry_run_json(capsys):
    rc = cli.main(["timer", "in", "90s", "--code", "print('x')", "--address", DEAD, "--dry-run", "--json",
                   "--arg=--flag"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True and out["fires_in_s"] == 90
    job = out["job"]
    assert list(job) == ["job_id", "title", "queue", "interpreter", "script", "args", "cwd", "timeout_s",
                         "wait_for_worker_s", "notify_channel", "cron"]
    assert job["script"] == "-c" and job["args"] == ["print('x')", "--flag"]
    assert job["job_id"].startswith("oneshot-code-")


def test_timer_command_dry_run(capsys):
    rc = cli.main(["timer", "in", "5m", "--dry-run", "--json", "--address", DEAD, "--command", "echo", "-n", "hi"])
    assert rc == 0
    job = json.loads(capsys.readouterr().out)["job"]
    assert job["job_id"].startswith("oneshot-command-")
    code = job["args"][0]
    assert code.startswith("import json,subprocess,sys;") and "echo" in code and "-n" in code


def test_timer_requires_exactly_one_mode(capsys):
    rc = cli.main(["timer", "in", "5m", "--dry-run", "--json", "--code", "1", "--script", "a.py"])
    assert rc == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_schedule_create_dry_run(capsys):
    rc = cli.main(["schedule", "create", "--id", "nightly", "--cron", "30 2 * * *", "--script", "r.py",
                   "--arg=--days", "--arg=1", "--address", DEAD, "--dry-run", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    text = json.dumps(out)
    assert "nightly" in text and "30 2 * * *" in text and "--days" in text


def test_schedule_apply_dry_run(capsys, tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"profiles": {"p": {"address": DEAD, "agent_ctl": "/opt/agent_ctl.py"}}}),
                   encoding="utf-8")
    rc = cli.main(["schedule", "apply", "--file", str(ROOT / "examples/jobs.example.json"),
                   "--config", str(cfg), "--profile", "p", "--dry-run", "--json"])
    assert rc == 0
    text = capsys.readouterr().out
    for jid in ("nightly-report", "heartbeat", "weekly-agent-review"):
        assert jid in text


def test_agent_job_without_ctl_is_usage_error(capsys):
    rc = cli.main(["timer", "in", "1m", "--session", "abc", "--message", "m", "--address", DEAD,
                   "--dry-run", "--json"])
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["error"] == "USAGE" and "agent_ctl" in out["message"]


def test_connect_failure_exit_code(capsys):
    rc = cli.main(["schedule", "list", "--address", DEAD, "--timeout", "2", "--json"])
    assert rc == 5
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_build_timer_job_session_ids():
    prof = Profile(name="t", agent_ctl="/opt/ctl.py", exec_queue="exe")
    job = timers.build_timer_job(prof, "2h", session="0123456789abcdef", message="wake", now=1000)
    assert job.job_id == "oneshot-session-01234567-1000"
    assert job.args == ["send", "0123456789abcdef", "--message", "wake"]
    assert job.queue == "exe"
    with pytest.raises(ValueError):
        timers.build_timer_job(prof, "2h", session="abc", now=1)


def test_build_timer_and_schedule_spawn_mode():
    prof = Profile(name="t", agent_ctl="/opt/ctl.py", exec_queue="exe", cwd="/work")
    job = timers.build_timer_job(
        prof,
        "5m",
        spawn=True,
        agent="claude",
        model="auto",
        effort="low",
        task_type="audit",
        title="ops · audit check",
        project="cto-legends",
        message="Reply OK",
        now=2000,
    )
    assert job.job_id == "oneshot-spawn-claude-2000"
    assert job.args == [
        "spawn",
        "--agent",
        "claude",
        "--workspace",
        "/work",
        "--title",
        "ops · audit check",
        "--model",
        "auto",
        "--type",
        "audit",
        "--stipend-depth",
        "1",
        "--message",
        "[stipend depth 1]\nReply OK",
        "--effort",
        "low",
        "--project",
        "cto-legends",
    ]
    d = schedules.expand_definition(
        {
            "job_id": "spawn-audit",
            "title": "ops · audit",
            "kind": "agent",
            "spawn": True,
            "agent": "gemini",
            "model": "auto",
            "effort": "high",
            "task_type": "audit",
            "cron": "0 7 * * *",
            "message": "Run audit",
        },
        prof,
    )
    wire, _ = schedules.split_definition(d)
    assert wire["args"][:4] == ["spawn", "--agent", "gemini", "--workspace"]
    assert "[stipend depth 1]\nRun audit" in wire["args"]
