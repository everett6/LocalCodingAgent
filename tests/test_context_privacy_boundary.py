"""Model-visible text is filtered without altering canonical local evidence."""
import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from local_coder.agents.base import BaseAgent
from local_coder.context.privacy import (
    ModelTextView, bounded_known_secrets, private_read_path,
    redact_diagnostic_value, redact_model_text,
)
from local_coder.core.runtime import AgentRuntime
from local_coder.orchestrator.coordinator import Coordinator
from local_coder.tools import create_tool_registry
from local_coder.tools.base import ToolRegistry
from local_coder.tools.filesystem import ReadFileTool
from local_coder.types import AgentEvent, AgentRole, AgentTask, Message, ModelConfig, ModelResponse, ProjectConfig, ToolCall, ToolResult


class InspectAgent(BaseAgent):
    role = AgentRole.CODER
    system_prompt = "Inspect only this local fixture."


@pytest.mark.parametrize("path", [
    ".env", ".env.local", ".env.example", ".git/config", ".local-coder/state/checkpoint.json",
    ".local-coder/state/context-archives/fixture.json", ".ssh/id_ed25519", ".aws/credentials",
    ".azure/profile.json", ".kube/config", ".npmrc", "credentials.json", "secrets.toml",
    "deploy/server.KEY", "deploy/key.pem", "deploy/key.p12", "deploy/key.pfx", "keys.keystore",
])
@pytest.mark.parametrize("role", [AgentRole.CODER, AgentRole.EXPLORER, AgentRole.SECURITY, AgentRole.ORCHESTRATOR])
def test_generic_private_reads_are_blocked_before_execution_for_every_role(tmp_path, path, role):
    class ForbiddenRead(ReadFileTool):
        async def execute(self, **kwargs):
            pytest.fail("Private content reached a generic file tool")

    registry = ToolRegistry()
    registry.project_root = str(tmp_path)
    registry.register(ForbiddenRead(str(tmp_path)))
    result = asyncio.run(registry.execute_tool(role, "read_file", {"path": path}))
    assert not result.success and result.data["execution_outcome"] == "not_started"
    assert result.data["private_read_blocked"] is True
    assert "security_scan" in result.output and "recover_context" in result.output
    assert path not in result.output


def test_private_path_checks_ignore_project_ancestors_and_cover_aliases(tmp_path):
    root = tmp_path / ".codex" / "worktrees" / "project"
    root.mkdir(parents=True)
    (root / "app.py").write_text("print('safe fixture')")
    (root / ".env").write_text("fixture-only")
    assert not private_read_path(root / "app.py", root)
    assert not private_read_path("src/credential_helpers.py", root)
    assert not private_read_path("tests/fixtures/security_cases.py", root)
    assert private_read_path("../outside.py", root)
    assert private_read_path(r".local-coder\state\archive.json", root)
    (root / "alias.txt").symlink_to(root / ".env")
    assert private_read_path("alias.txt", root)


def test_known_secret_binding_is_bounded_and_does_not_mask_short_words():
    values = bounded_known_secrets(["a", "short", "fixture-provider-key", "fixture-provider-key", "longer-fixture-provider-key", "x" * 4097])
    assert values == ("longer-fixture-provider-key", "fixture-provider-key")
    assert len(bounded_known_secrets(f"fixture-{number:03}" for number in range(100))) == 64
    assert redact_model_text("a short normal sentence", values) == "a short normal sentence"


def test_common_credentials_are_removed_without_changing_source_line_numbers():
    key = "sk-" + "x" * 40
    private = "-----BEGIN PRIVATE KEY-----\nfixture-only-material\n-----END PRIVATE KEY-----"
    text = f'api_key="fixture-only-key"\n{key}\nBearer fixture-bearer-value\n{private}\npostgres://user:pass@localhost/db\n'
    safe = redact_model_text(text)
    assert key not in safe and "fixture-only-key" not in safe and "fixture-only-material" not in safe
    assert "fixture-bearer-value" not in safe and "user:pass" not in safe
    assert safe.count("\n") == text.count("\n")
    assert 'api_key="[redacted]"' in safe
    assert redact_model_text('API_KEY = os.getenv("API_KEY")') == 'API_KEY = os.getenv("API_KEY")'


@pytest.mark.parametrize("expression", ["password = config.password", "authorization: authHeader", "api_key = provider_key", "token = parsed_token", "DB_PASSWORD = settings.password"])
def test_credential_variable_expressions_remain_legible(expression):
    assert redact_model_text(expression) == expression


@pytest.mark.parametrize("name", ["DB_PASSWORD", "client_api_key", "aws_secret_access_key", "service-access-token"])
def test_quoted_prefixed_credential_values_are_redacted(name):
    text = f'{name} = "private-fixture-value"'
    assert redact_model_text(text) == f'{name} = "[redacted]"'


def test_partial_pem_page_masks_material_to_end_without_masking_header_documentation():
    material = "A" * 64
    text = f"before\n-----BEGIN PRIVATE KEY-----\n{material}\nmore paged output\n"
    safe = redact_model_text(text)
    assert material not in safe and "more paged output" not in safe
    assert text.count("\n") == safe.count("\n")
    example = 'Recognize the literal "-----BEGIN PRIVATE KEY-----" in documentation.\nContinue reading normal code.'
    assert redact_model_text(example) == example
    standalone = "-----BEGIN PRIVATE KEY-----\nThis header identifies a PEM private key.\n"
    assert redact_model_text(standalone) == standalone


def test_many_unmatched_private_key_headers_preserve_documentation_and_remain_bounded():
    import time
    documentation = "-----BEGIN PRIVATE KEY-----\nThis documents the header without including key material.\n" * 1500
    started = time.perf_counter()
    assert redact_model_text(documentation) == documentation
    # A generous regression ceiling on a small fixture, paired with the
    # explicit one-pass implementation; this is not a latency SLA.
    assert time.perf_counter() - started < 3


def test_model_text_view_preserves_opaque_protocol_and_original_messages():
    secret = "fixture-provider-credential"
    original = [
        Message(role="system", content="Host context " + secret),
        Message(role="user", content="Source text " + secret),
        Message(role="assistant", content="opaque-compatible assistant content", reasoning_content=secret,
                reasoning_details=[{"type": "reasoning.encrypted", "data": secret}],
                tool_calls=[ToolCall(id="same-id", name="read_file", arguments={"path": secret})]),
        Message(role="tool", name="read_file", tool_call_id="same-id", content="Observation " + secret),
    ]
    before = [message.model_dump() for message in original]
    visible = ModelTextView(None, (secret,)).visible(original)
    assert all(secret not in message.content for message in visible if message.role != "assistant")
    assert visible[2] is original[2]
    assert visible[2].reasoning_content == secret and visible[2].reasoning_details[0]["data"] == secret
    assert visible[2].tool_calls[0].arguments["path"] == secret
    assert visible[3].tool_call_id == "same-id" and visible[3].name == "read_file"
    assert [message.model_dump() for message in original] == before


def test_host_model_replacement_is_used_by_privacy_and_the_real_agent_loop():
    class Model:
        config = SimpleNamespace(temperature=0., max_tokens=128)
        def __init__(self, content): self.content = content; self.calls = []
        async def generate(self, messages, **kwargs):
            self.calls.append(messages)
            return ModelResponse(content=self.content)
    class Registry:
        model_visible_secrets = ("fixture-provider-value",)
        def get_schemas_for_role(self, role): return []
    original = Model("Old model must not run")
    replacement = Model("Completed with the replacement model.")
    agent = InspectAgent(original, Registry())
    agent.model = replacement
    result = asyncio.run(agent.execute(AgentTask(role=AgentRole.CODER, objective="Inspect fixture-provider-value")))
    assert original.calls == [] and len(replacement.calls) == 1
    assert result.summary == "Completed with the replacement model."
    assert all("fixture-provider-value" not in message.content for message in replacement.calls[0])


def test_real_agent_model_receives_redacted_source_but_original_hashes_and_results_survive(tmp_path):
    secret = "opaque-fixture-provider-value-987654"
    raw = f'CONFIG_VALUE = "{secret}"\nprint("fixture")\n'
    (tmp_path / "app.py").write_text(raw)
    digest = hashlib.sha256(raw.encode()).hexdigest()

    class Model:
        config = SimpleNamespace(temperature=0., max_tokens=128, context_length=8192)
        calls = []
        async def generate(self, messages, **kwargs):
            self.calls.append(list(messages))
            if len(self.calls) == 1:
                return ModelResponse(tool_calls=[ToolCall(id="read-fixture", name="read_file", arguments={"path": "app.py"})])
            return ModelResponse(content="Inspected the fixture.")

    async def scenario():
        registry = create_tool_registry(str(tmp_path))
        registry.model_visible_secrets = (secret,)
        registry.runtime = AgentRuntime(str(tmp_path))
        result = await registry.execute_tool(AgentRole.CODER, "read_file", {"path": "app.py"})
        assert result.output == raw and result.data["source_hashes"]["app.py"] == digest
        model = Model()
        agent = InspectAgent(model, registry)
        task = AgentTask(role=AgentRole.CODER, objective="Inspect the fixture", context={"file_contents": {
            "app.py": raw, ".local-coder/state/context-archives/other.json": "foreign opaque fixture data",
        }})
        original_context = dict(task.context.file_contents)
        try:
            await agent.execute(task)
            assert len(model.calls) == 2
            for messages in model.calls:
                visible = "\n".join(message.content for message in messages if message.role != "assistant")
                assert secret not in visible and "foreign opaque fixture data" not in visible
            tool = next(message for message in model.calls[1] if message.role == "tool")
            assert "Credential text redacted" in tool.content
            assert agent._source_hashes["app.py"] == digest
            assert (tmp_path / "app.py").read_text() == raw and result.output == raw
            assert task.context.file_contents == original_context
            checkpoints = [json.loads(path.read_text()) for path in registry.runtime.state_dir.glob("checkpoint_*.json")]
            assert any(row["notes"]["workspace_file_fingerprints"].get("app.py") == digest for row in checkpoints)
        finally:
            await registry.close()
    asyncio.run(scenario())


def test_generic_read_cannot_bypass_foreign_role_archive_recovery(tmp_path):
    async def scenario():
        registry = create_tool_registry(str(tmp_path))
        registry.runtime = AgentRuntime(str(tmp_path))
        agent = InspectAgent(SimpleNamespace(), registry)
        reference = agent._archive_context([Message(role="assistant", content="owned evidence", reasoning_content="opaque-only-fixture")])
        try:
            generic = await registry.execute_tool(AgentRole.EXPLORER, "read_file", {"path": reference})
            foreign = await registry.execute_tool(AgentRole.EXPLORER, "recover_context", {"reference": reference})
            owned = await registry.execute_tool(AgentRole.CODER, "recover_context", {"reference": reference})
            assert not generic.success and not foreign.success
            assert owned.success and "owned evidence" in owned.output
            assert "opaque-only-fixture" not in owned.output
            assert "recover_context" in agent._discovered_tools
        finally:
            await registry.close()
    asyncio.run(scenario())


def test_diagnostics_preserve_numeric_usage_identity_and_original_event(tmp_path, monkeypatch):
    secret = "fixture-provider-known-value"
    monkeypatch.setenv("CLIN_PRIVACY_FIXTURE", secret)
    monkeypatch.setenv("UNRELATED_PRIVACY_FIXTURE", "do-not-bind-this-unconfigured-value")
    coordinator = Coordinator(ProjectConfig(models={"fixture": ModelConfig(name="fixture", model_id="fixture", backend="openai_compatible", api_key_env="CLIN_PRIVACY_FIXTURE")}), str(tmp_path))
    captured = []
    coordinator.on_event(captured.append)
    original = AgentEvent(source="FIXTURE", event_type="fixture", task_id="fixture-task", message="Backend error " + secret,
                          data={"task_id": "fixture-task", "prompt_tokens": 12, "completion_tokens": 3, "token": 15,
                                "api_key": "short", "detail": secret, "approval_id": "approval-fixture"})
    try:
        assert secret in coordinator.tool_registry.model_visible_secrets
        assert "do-not-bind-this-unconfigured-value" not in coordinator.tool_registry.model_visible_secrets
        coordinator._dispatch(original)
        event = captured[0]
        assert secret not in event.model_dump_json()
        assert event.source == original.source and event.task_id == "fixture-task"
        assert event.data["prompt_tokens"] == 12 and event.data["completion_tokens"] == 3 and event.data["token"] == 15
        assert event.data["task_id"] == "fixture-task" and event.data["approval_id"] == "approval-fixture"
        assert original.message.endswith(secret) and original.data["detail"] == secret
    finally:
        asyncio.run(coordinator.model_manager.close_all())


def test_returned_generation_error_and_agent_events_never_echo_bound_provider_key(tmp_path):
    secret = "opaque-fixture-provider-secret"
    class Model:
        config = SimpleNamespace(temperature=0., max_tokens=128)
        async def generate(self, messages, **kwargs):
            raise RuntimeError("Synthetic provider failure: " + secret)
    class Registry:
        model_visible_secrets = (secret,)
        def get_schemas_for_role(self, role): return []
    events = []
    agent = InspectAgent(Model(), Registry(), events.append)
    agent.generation_retry_limit = 0
    result = asyncio.run(agent.execute(AgentTask(role=AgentRole.CODER, objective="fixture only")))
    assert secret not in result.model_dump_json()
    assert all(secret not in event.model_dump_json() for event in events)
    assert "[redacted]" in " ".join(result.issues)
    assert redact_diagnostic_value({"input_tokens": 12, "password": "synthetic"}) == {"input_tokens": 12, "password": "[redacted]"}
