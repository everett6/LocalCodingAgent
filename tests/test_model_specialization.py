"""Identity, capability and permission invariants for shipped model scaffolds."""
import json

import pytest

from local_coder.models.profiles import resolve_profile
from local_coder.models.specialization import capability_facts, find_model, index_summary, model_index
from local_coder.types import AgentRole, ModelConfig


def config(model_id, **changes):
    return ModelConfig(name="test", model_id=model_id, backend="openai_compatible",
                       base_url="https://openrouter.ai/api/v1", **changes)


def test_snapshot_has_fifty_distinct_attributed_models_and_no_alias_collisions():
    rows = model_index()["models"]
    assert len(rows) == 50 == len({row["name"] for row in rows})
    assert [row["model_rank"] for row in rows] == list(range(1, 51))
    assert index_summary()["variant_count"] >= 50
    aliases = {}
    for row in rows:
        assert row["source_url"].startswith("https://artificialanalysis.ai/models/")
        for alias in [row["name"], *row["aliases"], *(v["name"] for v in row["variants"])]:
            assert aliases.setdefault(alias.casefold(), row["name"]) == row["name"]
            assert find_model(alias)["name"] == row["name"]


@pytest.mark.parametrize("row", model_index()["models"], ids=lambda row: row["name"])
def test_each_model_selects_three_distinct_scaffolds_without_changing_limits(row):
    selected = config(row.get("openrouter_id", row["name"]), context_length=65536, max_tokens=2048)
    before = selected.model_dump()
    profiles = [resolve_profile(selected, role) for role in
                (AgentRole.SECURITY, AgentRole.RED_TEAM, AgentRole.BUG_HUNTER)]
    assert all(profile.identity == row["name"] for profile in profiles)
    assert len({profile.workflow for profile in profiles}) == 3
    assert len({profile.preferred_tools for profile in profiles}) == 3
    assert len({profile.instructions for profile in profiles}) == 3
    for profile in profiles:
        assert profile.provenance["match"] == "exact_snapshot_identity"
        assert profile.provenance["model_rank"] == row["model_rank"]
        assert profile.max_strategy_attempts <= selected.strategy_attempt_limit
        assert len(profile.instructions) < 1000
        assert 5 <= profile.context_share_percent <= 20
        assert profile.catalog_char_limit <= 14000
        schemas = [{"function": {"name": name}} for name in ("read_file", "run_tests")]
        assert all(schema in schemas for schema in profile.apply_tools(schemas))
    assert selected.model_dump() == before  # no effort, spend, output or physical-window mutation


def test_unknown_release_and_family_hints_never_claim_index_membership():
    assert find_model("anthropic/claude-opus-5.5-unknown") is None
    profile = resolve_profile(config("qwen/unknown-release"), AgentRole.BUG_HUNTER)
    assert profile.family == "qwen" and profile.provenance["match"] == "family_heuristic"
    assert "model_rank" not in profile.describe()["provenance"]
    assert find_model("unrelated-claude-opus-5.5-copy") is None


@pytest.mark.parametrize("url", ["http://openrouter.ai/api/v1", "http://localhost:8000/v1",
                                     "https://other.example/api/v1", "https://openrouter.ai/other"])
def test_catalog_capabilities_do_not_leak_to_unverified_endpoints(url):
    row = next(row for row in model_index()["models"] if row.get("supported_parameters"))
    selected = config(row["openrouter_id"]).model_copy(update={"base_url": url})
    assert capability_facts(row, selected)["tools"] is None


def test_missing_tool_capability_narrows_even_an_explicit_overlay(monkeypatch):
    row = next(row for row in model_index()["models"] if row.get("openrouter_id"))
    monkeypatch.setattr("local_coder.models.profiles.capability_facts",
                        lambda row, config: {"status": "catalog_snapshot", "tools": False})
    profile = resolve_profile(config(row["openrouter_id"]), AgentRole.SECURITY,
                              {"tool_allowlist": ["read_file"]})
    assert profile.apply_tools([{"function": {"name": "read_file"}}]) == []


def test_profile_public_metadata_never_serializes_credentials_or_raw_unknown_names():
    selected = config("private-api-key-in-name", api_key="private-credential")
    serialized = json.dumps(resolve_profile(selected, AgentRole.SECURITY).describe())
    assert "private-api-key-in-name" not in serialized and "private-credential" not in serialized


def test_small_models_reduce_caps_without_overriding_physical_config():
    row = next(row for row in model_index()["models"] if row["family"] == "claude")
    small = resolve_profile(config(row["name"], context_length=8192), AgentRole.SECURITY)
    large = resolve_profile(config(row["name"], context_length=200000), AgentRole.SECURITY)
    assert small.catalog_char_limit < large.catalog_char_limit
    assert small.tool_result_char_limit < large.tool_result_char_limit
    assert small.context_share_percent <= large.context_share_percent


@pytest.mark.parametrize("field", ["catalog_char_limit", "context_share_percent", "tool_result_char_limit", "capabilities"])
def test_profile_overlays_cannot_edit_engine_capability_policy(field):
    with pytest.raises(ValueError):
        resolve_profile(config("unknown"), AgentRole.SECURITY, {field: 999999})
