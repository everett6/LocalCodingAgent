import asyncio
import json
import stat
import sys
import threading

import pytest

from local_coder.harness_api import HarnessAPI
from local_coder.types import HarnessSettings, SafetyTier


class FixtureAPI(HarnessAPI):
    def __init__(self, root):
        self.project_root = str(root)
        self.workspace = root
        self._job_lock = threading.Lock()
        self.active_job = None
        self.config_path = None
        self.config_overrides = []
        self._init_harness_api()


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CODER_USER_CONFIG", str(tmp_path / "no-user-config.yaml"))
    return FixtureAPI(tmp_path)


def test_default_status_is_local_and_unknown_routes_return_none(api, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Status must not launch subprocesses")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    result = api._harness_get("/harness", {})
    assert isinstance(api.harness_settings(), HarnessSettings)
    assert result["budget"] is None and result["settings"]["max_subagents"] == 3
    assert result["language"]["adapter_count"] >= 20
    assert api._harness_get("/integrations/mcp", {})["servers"] == {}
    assert api._harness_get("/not-a-route", {}) is None
    assert api._harness_post("/not-a-route", {}) is None


def test_harness_settings_persist_privately_and_validate_strict_types(api, tmp_path):
    result = api._harness_post(
        "/harness/configure",
        {
            "settings": {"memory_enabled": False, "max_requests": 12},
            "safety_tier": "tier_4_auto_valid",
        },
    )
    assert result["settings"]["max_requests"] == 12
    assert api.harness_safety_tier() == SafetyTier.TIER_4_AUTO_VALID
    path = tmp_path / ".local-coder/harness/settings.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    loaded = FixtureAPI(tmp_path)
    assert loaded.harness_settings().memory_enabled is False
    assert loaded.harness_settings().max_requests == 12
    for settings in [
        {"memory_enabled": "false"},
        {"max_requests": True},
        {"max_cost_usd": float("nan")},
        {"unknown": True},
        {"profile_overrides": {"approval": False}},
    ]:
        with pytest.raises(ValueError):
            api._harness_post("/harness/configure", {"settings": settings})
    assert api.harness_settings().max_requests == 12


def test_configuration_and_restore_cannot_race_an_active_job(api):
    api._job_lock.acquire()
    try:
        for route, body in [
            ("/harness/configure", {"settings": {}}),
            ("/integrations/mcp/configure", {"servers": {}}),
            ("/language/configure", {"server_commands": {}}),
            ("/snapshots/" + "a" * 32 + "/restore", {}),
        ]:
            with pytest.raises(ValueError, match="idle|active|running"):
                api._harness_post(route, body)
    finally:
        api._job_lock.release()


def test_memory_routes_index_search_correct_and_delete(api, tmp_path):
    (tmp_path / "app.py").write_text("def caramel_account():\n    return 1\n")
    assert api._harness_post("/memory/index", {})["files"] == 1
    records = api._harness_get("/memory", {"limit": ["20"]})["memories"]
    assert records
    found = api._harness_get(
        "/memory/search", {"q": ["caramel_account"], "budget": ["4000"]}
    )["items"]
    assert found
    identity = records[0]["id"]
    api._harness_post(
        f"/memory/{identity}/update", {"content": "marzipan repaired evidence"}
    )
    assert api._harness_get("/memory/search", {"q": ["marzipan"]})["items"]
    assert api._harness_post(f"/memory/{identity}/delete", {})["deleted"] is True
    assert not api._harness_get("/memory/search", {"q": ["marzipan"]})["items"]
    with pytest.raises(ValueError):
        api._harness_get("/memory", {"limit": ["-2"]})


def test_mcp_configure_is_explicit_and_probe_reaps_same_loop(api, tmp_path):
    script = tmp_path / "mcp.py"
    script.write_text("""import json,sys
for line in sys.stdin:
    msg=json.loads(line)
    if "id" not in msg:continue
    result={"protocolVersion":"2025-03-26","capabilities":{}} if msg["method"]=="initialize" else {"tools":[{"name":"read","inputSchema":{"type":"object"}}]}
    print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":result}),flush=True)
""")
    config = {
        "servers": {
            "local": {
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(script)],
                "enabled": True,
                "read_only_tools": ["read"],
            }
        }
    }
    assert (
        api._harness_post("/integrations/mcp/configure", config)["servers"]["local"][
            "connected"
        ]
        is False
    )
    assert (
        api._harness_post("/integrations/mcp/probe", {"server": "local"})["tools"][0][
            "name"
        ]
        == "read"
    )
    assert (
        api._harness_get("/integrations/mcp", {})["servers"]["local"]["connected"]
        is False
    )


def test_skills_routes_and_private_language_configuration(api, tmp_path):
    assert api._harness_get("/integrations/skills", {})["skills"]
    saved = api._harness_post(
        "/integrations/skills/save",
        {"skill": "team-review", "title": "Team review", "content": "Inspect callers"},
    )
    assert saved["id"] == "team-review"
    assert (
        api._harness_get("/integrations/skills/team-review", {})["content"]
        == "Inspect callers"
    )
    result = api._harness_post(
        "/language/configure",
        {"server_commands": {"python": None}, "auto_discover": False},
    )
    assert result["config"]["server_commands"] == {"python": None}
    path = tmp_path / ".local-coder/language/settings.json"
    assert json.loads(path.read_text())["auto_discover"] is False
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    (tmp_path / "app.py").write_text("def sample():\n    return 1\n")
    context = api._harness_get("/language/context", {"path": ["app.py"]})
    assert context["symbols"][0]["name"] == "sample"
    for body in [
        {"server_commands": {"python": ["python"]}},
        {"server_commands": {}, "auto_discover": "true"},
    ]:
        with pytest.raises(ValueError):
            api._harness_post("/language/configure", body)


def test_snapshot_routes_preserve_user_edits_and_hide_credentials(api, tmp_path):
    file = tmp_path / "app.py"
    file.write_text("value = 1\n")
    record = api.file_snapshots.capture(["app.py"])
    file.write_text("value = 2\n")
    api.file_snapshots.complete(record["id"])
    assert api._harness_get("/snapshots", {})["snapshots"]
    assert (
        "value = 2" in api._harness_get(f"/snapshots/{record['id']}/diff", {})["diff"]
    )
    file.write_text("user changed this\n")
    with pytest.raises(ValueError, match="changed"):
        api._harness_post(f"/snapshots/{record['id']}/restore", {})
    assert file.read_text() == "user changed this\n"
    private = tmp_path / ".env"
    private.write_text("PRIVATE=must-not-display")
    secret_snapshot = api.file_snapshots.capture([".env"])
    with pytest.raises(ValueError):
        api._harness_get(f"/snapshots/{secret_snapshot['id']}/diff", {})


def test_pending_approval_routes_require_a_real_boolean(api):
    async def scenario():
        waiting = asyncio.create_task(api.approvals.request("Review this local effect"))
        await asyncio.sleep(0.01)
        item = api._harness_get("/approvals", {})["approvals"][0]
        with pytest.raises(ValueError):
            api._harness_post(f"/approvals/{item['id']}/respond", {"approved": "true"})
        api._harness_post(f"/approvals/{item['id']}/respond", {"approved": False})
        assert await waiting is False
        assert api._harness_get("/approvals", {})["approvals"] == []

    asyncio.run(scenario())


def test_settings_paths_reject_symlinks(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CODER_USER_CONFIG", str(tmp_path / "no-user-config.yaml"))
    other = tmp_path / "elsewhere"
    other.mkdir()
    (tmp_path / ".local-coder").symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        FixtureAPI(tmp_path)


def test_rejects_boolean_budget_inputs_and_encoded_path_traversal(api):
    for path, body in [
        ("/language/context", {"path": "app.py", "line": True}),
        (
            "/integrations/skills/save",
            {"skill": "../elsewhere", "title": "bad", "content": "data"},
        ),
        ("/harness/configure", {"settings": {}, "approved": True}),
        ("/memory/index", {"paths": ["../elsewhere"]}),
    ]:
        with pytest.raises(ValueError):
            api._harness_post(path, body)
    with pytest.raises(ValueError):
        api._harness_get("/integrations/skills/%2E%2E%2Fsecret", {})
    with pytest.raises(ValueError):
        api._harness_get("/memory/search", {"q": ["one", "two"]})


def test_api_budget_status_omits_internal_entries(api):
    from local_coder.core.budgets import BudgetLedger

    api.current_budget = BudgetLedger(max_requests=7)
    result = api._harness_get("/harness", {})
    assert result["budget"]["limits"]["max_requests"] == 7
    assert "entries" not in result["budget"]


def test_snapshot_diff_redacts_credentials_embedded_in_source(api, tmp_path):
    file = tmp_path / "client.py"
    file.write_text('api_key = "sk-' + "x" * 32 + '"\n')
    snapshot = api.file_snapshots.capture(["client.py"])
    result = api._harness_get(f"/snapshots/{snapshot['id']}/diff", {})
    assert "sk-" + "x" * 32 not in json.dumps(result)
