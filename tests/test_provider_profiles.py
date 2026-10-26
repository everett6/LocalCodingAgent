import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

from local_coder.providers.profiles import ProviderProfileStore


def provider(**changes):
    return {
        "backend": "openai_compatible",
        "model_id": "~vendor/model-latest",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "PROFILE_PROVIDER_KEY",
        "context_length": 262144,
        "max_tokens": 65536,
        "profile": "generic",
        **changes,
    }


def test_profile_save_get_update_delete_persist_without_credentials(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PROFILE_PROVIDER_KEY", "resolved-private-credential")
    store = ProviderProfileStore(tmp_path)
    assert store.list() == []
    record = store.save(
        "My model",
        provider(
            api_key="raw-private-credential",
            headers={"Authorization": "Bearer raw-private-credential"},
            extra={"nested": {"secret": "hidden-private-value"}},
            auto_context=True,
            context_source="catalog",
            request_cost_usd=0,
        ),
    )
    assert len(record["id"]) == 32 and record["name"] == "My model"
    assert record["provider"]["auto_context"] is True
    assert record["provider"]["request_cost_usd"] == 0
    path = tmp_path / ".local-coder/providers/profiles.json"
    serialized = path.read_text()
    for secret in (
        "raw-private-credential",
        "resolved-private-credential",
        "hidden-private-value",
        "Authorization",
    ):
        assert secret not in serialized
    if os.name != 'nt':
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    else:
        assert path.stat().st_mode & stat.S_IWRITE
    assert (
        ProviderProfileStore(tmp_path).get(record["id"])["provider"]["api_key_env"]
        == "PROFILE_PROVIDER_KEY"
    )
    changed = store.save("Renamed", provider(model_id="vendor/other"), id=record["id"])
    assert changed["id"] == record["id"] and len(store.list()) == 1
    assert store.get(record["id"])["name"] == "Renamed"
    assert store.delete(record["id"]) is True and store.delete(record["id"]) is False
    with pytest.raises(ValueError):
        store.get(record["id"])


@pytest.mark.parametrize(
    "changes",
    [
        {"base_url": "http://example.com/v1"},
        {"base_url": "https://u:p@example.com/v1"},
        {"base_url": "https://example.com/v1?token=secret"},
        {"model_id": "bad\nmodel"},
        {"context_length": True},
        {"auto_context": "true"},
        {"request_cost_usd": -1},
        {"request_cost_usd": float("nan")},
        {"api_key_env": "literal token value"},
        {"context_source": {"token": "secret"}},
    ],
)
def test_invalid_profiles_are_rejected_without_writes(tmp_path, changes):
    store = ProviderProfileStore(tmp_path)
    with pytest.raises(ValueError):
        store.save("Invalid", provider(**changes))
    assert store.list() == []


def test_known_raw_or_resolved_secrets_cannot_migrate_into_names_or_model_ids(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PROFILE_PROVIDER_KEY", "explicit-resolved-key")
    store = ProviderProfileStore(tmp_path)
    with pytest.raises(ValueError):
        store.save("explicit-resolved-key", provider())
    with pytest.raises(ValueError):
        store.save(
            "Bad", provider(model_id="raw-secret-value", api_key="raw-secret-value")
        )


def test_saved_local_path_identity_and_endpoint_are_preserved(tmp_path):
    record = ProviderProfileStore(tmp_path).save(
        "Local",
        provider(
            base_url="http://127.0.0.1:8090/v1",
            model_id="../models/local model.gguf",
            api_key_env=None,
        ),
    )
    assert record["provider"]["model_id"] == "../models/local model.gguf"


def test_local_unknown_request_price_and_blank_environment_reference_are_preserved(
    tmp_path,
):
    store = ProviderProfileStore(tmp_path)
    record = store.save(
        "Local",
        provider(
            base_url="http://127.0.0.1:8090/v1", api_key_env="", request_cost_usd=None
        ),
    )
    saved = store.get(record["id"])["provider"]
    assert saved["api_key_env"] is None and saved["request_cost_usd"] is None


def test_profile_state_rejects_symlinks_hardlinks_and_id_traversal(tmp_path):
    store = ProviderProfileStore(tmp_path)
    for identity in ("../escape", "a" * 31, "g" * 32):
        with pytest.raises(ValueError):
            store.get(identity)
    directory = tmp_path / ".local-coder/providers"
    directory.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.json"
    outside.write_text('{"profiles":[]}')
    target = directory / "profiles.json"
    target.symlink_to(outside)
    with pytest.raises(ValueError):
        store.list()
    target.unlink()
    os.link(outside, target)
    with pytest.raises(ValueError):
        store.save("Bad", provider())


def test_independent_store_instances_serialize_concurrent_saves(tmp_path):
    def save(index):
        return ProviderProfileStore(tmp_path).save(f"Model {index}", provider())

    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(save, range(16)))
    assert len({record["id"] for record in records}) == 16
    assert len(ProviderProfileStore(tmp_path).list()) == 16
    json.loads((tmp_path / ".local-coder/providers/profiles.json").read_text())


def test_profile_count_is_bounded_without_discarding_existing_records(tmp_path):
    store = ProviderProfileStore(tmp_path)
    for index in range(64):
        store.save(f"Model {index}", provider())
    with pytest.raises(ValueError):
        store.save("One too many", provider())
    assert len(store.list()) == 64
