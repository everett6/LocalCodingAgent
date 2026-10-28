import asyncio
import hashlib
import json
import os
import sys
import threading
from types import SimpleNamespace

import pytest

from local_coder.context.token_count import LocalTokenCounter, request_accounting, tokenizer_config
from local_coder.context_accounting_api import ContextAccountingAPI, load_counter_for_project
from local_coder.core.budgets import BudgetExceeded, BudgetLedger, BudgetedModel
from local_coder.harness_api import HarnessAPI


class Tokenizer:
    chat_template = "{{ roles }} {{ content }} {{ tools }} <assistant>"

    def apply_chat_template(self, messages, *, tokenize, add_generation_prompt, tools=None):
        assert tokenize and add_generation_prompt
        # Deterministic local fixture includes role/envelope/tool framing.
        return list(json.dumps({"messages": messages, "tools": tools}).encode()) + [1, 2, 3]


def counter(tokenizer=None, model="fixture"):
    tokenizer = tokenizer or Tokenizer()
    return LocalTokenCounter("unused", model, hashlib.sha256(tokenizer.chat_template.encode()).hexdigest(), tokenizer=tokenizer)


def config(**kwargs):
    return SimpleNamespace(model_id="fixture", input_cost_per_million=1, output_cost_per_million=2,
                           request_cost_usd=0, **kwargs)


@pytest.mark.parametrize("content", ["中文日本語", "🤖 café e\u0301", "\n\x00\"\\"])
def test_unicode_and_tool_schema_reserves_preserve_complete_json_bound(content):
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": content}]
    tools = [{"type": "function", "function": {"name": "read", "description": content,
             "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]
    result = request_accounting(messages, tools, 97, config(), counter())
    expected = len(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False).encode()) + 512
    assert result["prompt_reserved"] >= expected
    assert result["tool_schema_reserved"] == len(json.dumps(tools, ensure_ascii=False).encode())
    assert result["output_reserved"] == 97 and result["semantics"] == "estimated"
    assert result["component_method"] == "exact_local_chat_template"
    assert result["component_tokens"] == len(Tokenizer().apply_chat_template(messages, tools=tools, tokenize=True, add_generation_prompt=True))


def test_model_or_template_mismatch_and_tokenizer_failures_fall_back():
    local = counter()
    for selected in ("different", "fixture"):
        if selected == "fixture":
            local.template_sha256 = "0" * 64
        result = request_accounting([], None, 1, SimpleNamespace(model_id=selected), local)
        assert result["method"] == "conservative_utf8" and "component_tokens" not in result
    class Broken(Tokenizer):
        def apply_chat_template(self, *args, **kwargs):
            raise ValueError("secret must not escape")
    result = request_accounting([], None, 1, config(), counter(Broken()))
    assert "secret" not in json.dumps(result)
    assert result["method"] == "conservative_utf8"


def test_ignored_tools_do_not_claim_exact_component():
    class IgnoresTools(Tokenizer):
        def apply_chat_template(self, *args, **kwargs):
            return [1, 2]
    tools = [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}]
    result = request_accounting([], tools, 1, config(), counter(IgnoresTools()))
    assert result["method"] == "conservative_utf8"
    assert "tool schemas" in result["reason"]


def test_output_boundary_and_component_measurement_never_reduce_admission():
    messages = [{"role": "user", "content": "中文"}]
    accounting = request_accounting(messages, None, 10, config())
    bound = accounting["prompt_reserved"] + 10
    ledger = BudgetLedger(max_request_tokens=bound, token_counter=counter())
    identity = ledger.reserve(config(), messages, 10)
    with pytest.raises(BudgetExceeded, match="Per-request"):
        ledger.reserve(config(), messages, 11)
    ledger.settle(identity, SimpleNamespace(prompt_tokens=2, completion_tokens=3), config=config())
    entry = ledger.accounting_status()["entries"][0]
    assert entry["tokens_reserved"] == bound and entry["tokens"] == 5
    assert entry["accounting"]["output_reserved"] == 10
    assert entry["provider_actual"]["source"] == "provider_reported"
    class Huge(Tokenizer):
        def apply_chat_template(self, *args, **kwargs):
            return [1] * 5000
    with pytest.raises(BudgetExceeded):
        BudgetLedger(max_request_tokens=bound, token_counter=counter(Huge())).reserve(config(), messages, 10)


def test_local_counts_do_not_bypass_dollar_or_quota_gate():
    ledger = BudgetLedger(max_cost_usd=.000001, token_counter=counter())
    with pytest.raises(BudgetExceeded, match="cost"):
        ledger.reserve(config(), [], 10)
    calls = []
    class Quota:
        def admit(self, config):
            raise RuntimeError("quota exhausted")
    class Model:
        config = config()
        async def generate(self, *args, **kwargs):
            calls.append(True)
    ledger = BudgetLedger(token_counter=counter())
    with pytest.raises(RuntimeError, match="quota"):
        asyncio.run(BudgetedModel(Model(), ledger, Quota()).generate([], max_tokens=10))
    assert calls == [] and ledger.status()["requests"] == 0


def test_loader_uses_only_local_files_without_remote_code(monkeypatch, tmp_path):
    seen = []
    def load(path, **kwargs):
        seen.append((path, kwargs))
        return Tokenizer()
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(AutoTokenizer=SimpleNamespace(from_pretrained=load)))
    local = LocalTokenCounter(tmp_path, "fixture", "0" * 64)
    assert local.status()["available"]
    assert seen == [(str(tmp_path), {"local_files_only": True, "trust_remote_code": False})]


class API(ContextAccountingAPI, HarnessAPI):
    def __init__(self, root):
        self.workspace = root
        self.config_overrides = []
        self._job_lock = threading.Lock()
        self.active_job = None
        self._init_harness_api()
        self._init_context_accounting_api()


def test_api_is_idle_guarded_workspace_contained_and_redacts_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CODER_USER_CONFIG", str(tmp_path / "absent.yaml"))
    api = API(tmp_path)
    monkeypatch.setattr("local_coder.context_accounting_api.adapter_availability", lambda: {"installed": False})
    assert api._context_accounting_get("/context/accounting", {})["tokenizer"]["configured"] is False
    assert api._context_accounting_get("/unknown", {}) is None
    assert api._context_accounting_post("/unknown", {}) is None
    with pytest.raises(ValueError, match="query"):
        api._context_accounting_get("/context/accounting", {"unexpected": ["1"]})
    assert load_counter_for_project(tmp_path) is None
    folder = tmp_path / "local-tokenizer"
    folder.mkdir()
    body = {"path": folder.name, "model_id": "fixture", "template_sha256": "0" * 64}
    for path in ("../", "/tmp"):
        with pytest.raises(ValueError, match="workspace"):
            tokenizer_config(tmp_path, {**body, "path": path})
    api.active_job = "busy"
    with pytest.raises(ValueError, match="idle"):
        api._context_accounting_post("/context/tokenizer", body)
    api.active_job = None
    monkeypatch.setattr("local_coder.context_accounting_api.LocalTokenCounter", lambda *args: counter())
    monkeypatch.setattr("local_coder.context.token_count.LocalTokenCounter", lambda *args: counter())
    assert api._context_accounting_post("/context/tokenizer", body)["tokenizer"]["available"]
    assert load_counter_for_project(tmp_path).status()["available"]
    assert (tmp_path / ".local-coder/context/tokenizer.json").stat().st_mode & 0o777 == 0o600
    api.current_budget = BudgetLedger(token_counter=counter())
    for _ in range(110):
        api.current_budget.reserve(SimpleNamespace(model_id="super-secret-id"), [{"role": "user", "content": "private-content"}], 1)
    status = api._context_accounting_get("/context/accounting", {})
    assert len(status["ledger"]["entries"]) == 50
    assert "super-secret-id" not in json.dumps(status) and "private-content" not in json.dumps(status)
    status["ledger"]["entries"][0]["accounting"]["reason"] = "changed"
    assert api.current_budget.accounting_status()["entries"][0]["accounting"]["reason"] != "changed"
    assert api._context_accounting_post("/context/tokenizer", {"enabled": False})["tokenizer"]["configured"] is False
    assert load_counter_for_project(tmp_path) is None


def test_saved_deleted_tokenizer_is_nonfatal_and_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CODER_USER_CONFIG", str(tmp_path / "absent.yaml"))
    api = API(tmp_path)
    api._write_harness_state(".local-coder/context/tokenizer.json", {
        "enabled": True, "path": "deleted-directory", "model_id": "fixture", "template_sha256": "0" * 64,
    })
    loaded = API(tmp_path)
    assert loaded.context_token_counter() is None
    assert load_counter_for_project(tmp_path) is None
    assert "unavailable" in loaded._context_accounting_get("/context/accounting", {})["tokenizer"]["reason"]
    assert loaded._context_accounting_post("/context/tokenizer", {"enabled": False})["tokenizer"]["configured"] is False


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "public", "oversized", "malformed"])
def test_project_counter_refuses_unsafe_saved_files(tmp_path, unsafe):
    folder = tmp_path / ".local-coder/context"
    folder.mkdir(parents=True)
    target = folder / "tokenizer.json"
    target.write_text('{"enabled":false}')
    target.chmod(0o600)
    if unsafe == "symlink":
        actual = tmp_path / "actual"
        target.rename(actual)
        target.symlink_to(actual)
    elif unsafe == "hardlink":
        os.link(target, tmp_path / "copy")
    elif unsafe == "public":
        target.chmod(0o644)
    elif unsafe == "oversized":
        target.write_text("x" * 64001)
    else:
        target.write_text("invalid json")
    assert load_counter_for_project(tmp_path) is None


@pytest.mark.parametrize('unsafe', ['symlink', 'hardlink', 'directory_link', 'nonregular',
                                   'oversized_config', 'oversized_template', 'oversized_vocabulary',
                                   'too_many', 'total_bytes', 'external_reference', 'weights'])
def test_local_tokenizer_rejects_unsafe_assets_before_loader(monkeypatch, tmp_path, unsafe):
    folder = tmp_path / 'tokenizer'
    folder.mkdir()
    outside = tmp_path / 'outside.json'
    outside.write_text('{}')
    if unsafe == 'symlink':
        (folder / 'tokenizer_config.json').symlink_to(outside)
    elif unsafe == 'hardlink':
        os.link(outside, folder / 'tokenizer_config.json')
    elif unsafe == 'directory_link':
        (folder / 'chat_templates').symlink_to(tmp_path, target_is_directory=True)
    elif unsafe == 'nonregular':
        os.mkfifo(folder / 'vocab.txt')
    elif unsafe.startswith('oversized_'):
        filename, limit = {'oversized_config': ('tokenizer_config.json', 1024 * 1024),
                           'oversized_template': ('chat_template.jinja', 256 * 1024),
                           'oversized_vocabulary': ('tokenizer.json', 32 * 1024 * 1024)}[unsafe]
        with (folder / filename).open('wb') as stream:
            stream.truncate(limit + 1)
    elif unsafe == 'too_many':
        (folder / 'chat_templates').mkdir()
        for index in range(65):
            (folder / 'chat_templates' / f'{index}.jinja').write_text('')
    elif unsafe == 'total_bytes':
        for name in ('tokenizer.json', 'vocab.json', 'merges.txt'):
            with (folder / name).open('wb') as stream:
                stream.truncate(32 * 1024 * 1024)
    elif unsafe == 'external_reference':
        (folder / 'tokenizer_config.json').write_text(json.dumps({'sp_model_kwargs': {'model_file': '../outside.json'}}))
    elif unsafe == 'weights':
        (folder / 'model.safetensors').write_text('not a tokenizer asset')
    seen = []
    monkeypatch.setitem(sys.modules, 'transformers', SimpleNamespace(AutoTokenizer=SimpleNamespace(
        from_pretrained=lambda *args, **kwargs: seen.append(True))))
    local = LocalTokenCounter(folder, 'fixture', '0' * 64)
    assert not local.status()['available']
    assert seen == []


def test_valid_bounded_tokenizer_assets_are_usable(monkeypatch, tmp_path):
    (tmp_path / 'tokenizer_config.json').write_text(json.dumps({'chat_template': Tokenizer.chat_template}))
    (tmp_path / 'tokenizer.json').write_text('{}')
    (tmp_path / 'chat_templates').mkdir()
    (tmp_path / 'chat_templates' / 'tools.jinja').write_text('{{ tools }}')
    monkeypatch.setitem(sys.modules, 'transformers', SimpleNamespace(AutoTokenizer=SimpleNamespace(
        from_pretrained=lambda *args, **kwargs: Tokenizer())))
    local = LocalTokenCounter(tmp_path, 'fixture', hashlib.sha256(Tokenizer.chat_template.encode()).hexdigest())
    assert local.status()['available']
    assert request_accounting([], None, 1, config(), local)['component_tokens'] > 0


@pytest.mark.parametrize('kind', ['messages', 'tools', 'bytes', 'template'])
def test_oversized_inputs_never_reach_local_chat_template(kind):
    class NeverCalled(Tokenizer):
        def apply_chat_template(self, *args, **kwargs):
            pytest.fail('Oversized input must retain conservative accounting without tokenization')
    tokenizer = NeverCalled()
    if kind == 'template':
        tokenizer.chat_template = 'x' * (256 * 1024 + 1)
    local = counter(tokenizer)
    messages = [{'role': 'user', 'content': 'normal'}]
    tools = None
    if kind == 'messages':
        messages *= 257
    elif kind == 'tools':
        tools = [{'type': 'function', 'function': {'name': 'read', 'parameters': {}}}] * 129
    elif kind == 'bytes':
        messages = [{'role': 'user', 'content': 'x' * 2_000_000}]
    measured = request_accounting(messages, tools, 1, config(), local)
    conservative = request_accounting(messages, tools, 1, config())
    assert measured['prompt_reserved'] == conservative['prompt_reserved']
    assert measured['method'] == 'conservative_utf8' and 'component_tokens' not in measured
    assert 'limit' in measured['reason']
    if kind == 'bytes':
        with pytest.raises(BudgetExceeded, match='Per-request'):
            BudgetLedger(max_request_tokens=100, token_counter=local).reserve(config(), messages, 1)


def test_tokenizer_directory_symlink_is_rejected_even_inside_workspace(tmp_path):
    real = tmp_path / 'assets'
    real.mkdir()
    (tmp_path / 'linked').symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match='links'):
        tokenizer_config(tmp_path, {'path': 'linked', 'model_id': 'fixture', 'template_sha256': '0' * 64})


def test_oversized_inline_chat_template_rejected_before_loading(monkeypatch, tmp_path):
    (tmp_path / 'tokenizer_config.json').write_text(json.dumps({'chat_template': 'x' * (256 * 1024 + 1)}))
    def forbidden(*args, **kwargs):
        pytest.fail('Oversized inline templates cannot enter the loader')
    monkeypatch.setitem(sys.modules, 'transformers', SimpleNamespace(AutoTokenizer=SimpleNamespace(from_pretrained=forbidden)))
    assert not LocalTokenCounter(tmp_path, 'fixture', '0' * 64).status()['available']
