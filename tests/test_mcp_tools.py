"""Stdio MCP server: protocol, tool schemas, preview gating (offline)."""
from __future__ import annotations

import io
import json
from pathlib import Path

from legends_temporal import mcp_server
from legends_temporal.config import Profile

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT / "docs/CLI-CONTRACT.json").read_text(encoding="utf-8"))
PROFILE = Profile(name="t", address="127.0.0.1:9", workflow_queue="ctl", exec_queue="exe")


def rpc(method, params=None, mid=1):
    return mcp_server.handle_message({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}, PROFILE)


def test_initialize_and_ping():
    r = rpc("initialize", {"protocolVersion": "2025-06-18"})
    assert r["result"]["serverInfo"] == {"name": "legends-temporal", "version": "0.1.1"}
    assert r["result"]["protocolVersion"] == "2025-06-18"
    assert "tools" in r["result"]["capabilities"]
    assert rpc("ping")["result"] == {}


def test_notifications_get_no_response():
    assert mcp_server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, PROFILE) is None


def test_tools_list_matches_contract_and_schemas_are_strict():
    tools = rpc("tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == CONTRACT["mcp_tools"]
    for t in tools:
        s = t["inputSchema"]
        assert s["type"] == "object" and s.get("additionalProperties") is False, t["name"]
        assert t["description"]
        for req in s.get("required", []):
            assert req in s["properties"]


def test_unknown_method_and_tool():
    assert rpc("nope")["error"]["code"] == -32601
    assert rpc("tools/call", {"name": "nope"})["error"]["code"] == -32602


def test_timer_preview_without_confirm_never_connects():
    r = rpc("tools/call", {"name": "temporal_timer_schedule",
                           "arguments": {"delay": "10m", "code": "print('hi')", "notify_channel": "lab"}})
    res = r["result"]
    assert res["isError"] is False
    body = json.loads(res["content"][0]["text"])
    assert body["preview"] is True and body["fires_in_s"] == 600
    assert body["job"]["queue"] == "exe" and body["job"]["notify_channel"] == "lab"
    assert body["workflow_queue"] == "ctl"


def test_bad_arguments_are_tool_errors():
    r = rpc("tools/call", {"name": "temporal_timer_schedule", "arguments": {"delay": "soon", "code": "1"}})
    assert r["result"]["isError"] is True


def test_live_tool_unreachable_server_is_tool_error():
    r = rpc("tools/call", {"name": "temporal_schedule_list", "arguments": {}})
    assert r["result"]["isError"] is True
    assert json.loads(r["result"]["content"][0]["text"])["ok"] is False


def test_serve_loop_round_trip():
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
             {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    stdin = io.StringIO("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    stdout = io.StringIO()
    assert mcp_server.serve(PROFILE, stdin=stdin, stdout=stdout) == 0
    out = [json.loads(x) for x in stdout.getvalue().splitlines() if x.strip()]
    assert [o.get("id") for o in out] == [1, 2, None]
    assert out[2]["error"]["code"] == -32700
