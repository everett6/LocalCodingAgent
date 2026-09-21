"""Tests for the core types module."""
import json
from datetime import datetime

import pytest
from local_coder.types import (
    AgentEvent,
    AgentMetrics,
    AgentResponse,
    AgentRole,
    AgentTask,
    ApprovalConfig,
    FilePatch,
    GPUStatus,
    Message,
    ModelBackend,
    ModelConfig,
    ModelResponse,
    ProjectConfig,
    ResourceConfig,
    TaskContext,
    TaskNode,
    TaskPlan,
    TaskStatus,
    TestResult,
    ToolCall,
    ToolName,
    ToolResult,
    VerificationConfig,
)


class TestEnums:
    def test_agent_roles(self):
        assert AgentRole.ORCHESTRATOR.value == "orchestrator"
        assert AgentRole.CODER.value == "coder"
        assert len(AgentRole) == 7

    def test_task_status(self):
        assert TaskStatus.PENDING.value == "pending"
        assert TaskStatus.COMPLETED.value == "completed"

    def test_model_backend(self):
        assert ModelBackend.OLLAMA.value == "ollama"
        assert ModelBackend.LLAMACPP.value == "llama.cpp"
        assert ModelBackend.OPENAI_COMPAT.value == "openai_compatible"

    def test_tool_names(self):
        assert ToolName.READ_FILE.value == "read_file"
        assert ToolName.WRITE_FILE.value == "write_file"
        assert len(ToolName) >= 14


class TestMessages:
    def test_message_basic(self):
        msg = Message(role="user", content="Hello")
        assert msg.role == "user"
        assert msg.content == "Hello"
        assert msg.tool_calls is None

    def test_message_with_tool_calls(self):
        tc = ToolCall(name="read_file", arguments={"path": "foo.py"})
        msg = Message(role="assistant", content="", tool_calls=[tc])
        assert len(msg.tool_calls) == 1
        assert msg.tool_calls[0].name == "read_file"

    def test_tool_call_default_id(self):
        tc = ToolCall(name="test", arguments={})
        assert tc.id  # Should have auto-generated ID
        assert len(tc.id) == 8

    def test_tool_result(self):
        result = ToolResult(
            tool_call_id="abc",
            success=True,
            output="file contents",
            duration_ms=42.5,
        )
        assert result.success
        assert result.duration_ms == 42.5
        assert result.files_changed == []


class TestModelTypes:
    def test_model_response(self):
        resp = ModelResponse(
            content="Hello",
            prompt_tokens=10,
            completion_tokens=5,
            latency_ms=100.0,
        )
        assert resp.total_tokens == 0  # Not auto-computed
        assert resp.finish_reason == "stop"

    def test_model_config(self):
        config = ModelConfig(
            name="test",
            model_id="qwen2.5-coder:7b",
            backend=ModelBackend.OLLAMA,
        )
        assert config.context_length == 8192
        assert config.temperature == 0.2
        assert config.base_url == "http://localhost:11434"

    def test_model_config_custom(self):
        config = ModelConfig(
            name="custom",
            model_id="custom-model",
            backend=ModelBackend.OPENAI_COMPAT,
            base_url="http://localhost:1234/v1",
            context_length=4096,
            temperature=0.5,
            estimated_vram_mb=2000,
        )
        assert config.backend == ModelBackend.OPENAI_COMPAT
        assert config.estimated_vram_mb == 2000


class TestAgentProtocol:
    def test_agent_task_defaults(self):
        task = AgentTask(
            role=AgentRole.CODER,
            objective="Implement feature X",
        )
        assert task.task_id  # Auto-generated
        assert task.files == []
        assert task.constraints == []
        assert task.depends_on == []
        assert task.max_retries == 3

    def test_agent_task_full(self):
        task = AgentTask(
            role=AgentRole.CODER,
            objective="Add authentication",
            files=["src/auth.py"],
            constraints=["Don't change the API"],
            success_criteria=["Tests pass"],
            depends_on=["task-1"],
            priority=5,
        )
        assert task.priority == 5
        assert len(task.depends_on) == 1

    def test_task_context(self):
        ctx = TaskContext(
            architecture="MVC pattern",
            file_contents={"main.py": "import foo"},
        )
        assert ctx.architecture == "MVC pattern"
        assert "main.py" in ctx.file_contents

    def test_agent_response(self):
        resp = AgentResponse(
            task_id="task-1",
            status=TaskStatus.COMPLETED,
            summary="Done",
            files_changed=["a.py", "b.py"],
            tests_passed=True,
        )
        assert resp.status == TaskStatus.COMPLETED
        assert len(resp.files_changed) == 2
        assert not resp.follow_up_required

    def test_file_patch(self):
        patch = FilePatch(
            file_path="src/foo.py",
            new_content="new code",
            action="modify",
        )
        assert patch.action == "modify"

    def test_agent_metrics(self):
        m = AgentMetrics(model_calls=5, tool_calls=10)
        assert m.model_calls == 5
        assert m.latency_ms == 0.0


class TestTaskDAGTypes:
    def test_task_node(self):
        task = AgentTask(role=AgentRole.CODER, objective="test")
        node = TaskNode(task=task)
        assert node.status == TaskStatus.PENDING
        assert node.result is None
        assert node.retries == 0

    def test_task_plan(self):
        plan = TaskPlan(
            objective="Build API",
            tasks=[
                AgentTask(role=AgentRole.CODER, objective="Implement endpoints"),
                AgentTask(role=AgentRole.TESTER, objective="Write tests"),
            ],
            notes=["Consider rate limiting"],
        )
        assert len(plan.tasks) == 2
        assert plan.plan_id  # Auto-generated


class TestConfiguration:
    def test_resource_config_defaults(self):
        rc = ResourceConfig()
        assert rc.max_concurrent_gpu_agents == 1
        assert rc.max_concurrent_cpu_agents == 4
        assert rc.gpu_memory_target_percent == 85.0

    def test_approval_config(self):
        ac = ApprovalConfig()
        assert ac.require_approval_for_commands is True
        assert ac.require_approval_for_commits is True

    def test_verification_config(self):
        vc = VerificationConfig()
        assert vc.max_fix_iterations == 3

    def test_project_config_defaults(self):
        pc = ProjectConfig()
        assert pc.models == {}
        assert pc.log_level == "INFO"

    def test_project_config_with_models(self):
        pc = ProjectConfig(
            models={
                "coder": ModelConfig(
                    name="coder",
                    model_id="qwen:7b",
                ),
            }
        )
        assert "coder" in pc.models
        assert pc.models["coder"].model_id == "qwen:7b"


class TestEvents:
    def test_agent_event(self):
        event = AgentEvent(
            source="ORCHESTRATOR",
            event_type="task_started",
            message="Processing request",
            task_id="task-1",
        )
        assert event.source == "ORCHESTRATOR"
        assert event.task_id == "task-1"
        assert isinstance(event.timestamp, datetime)

    def test_gpu_status(self):
        status = GPUStatus(
            total_vram_mb=12288,
            used_vram_mb=4096,
            free_vram_mb=8192,
        )
        assert status.free_vram_mb == 8192


class TestSerialization:
    def test_agent_task_json_roundtrip(self):
        task = AgentTask(
            role=AgentRole.CODER,
            objective="Test serialization",
            files=["a.py"],
        )
        data = task.model_dump()
        restored = AgentTask.model_validate(data)
        assert restored.role == task.role
        assert restored.objective == task.objective

    def test_agent_response_json(self):
        resp = AgentResponse(
            task_id="t1",
            status=TaskStatus.COMPLETED,
            summary="Done",
        )
        json_str = resp.model_dump_json()
        parsed = json.loads(json_str)
        assert parsed["status"] == "completed"

    def test_model_config_json(self):
        config = ModelConfig(name="test", model_id="qwen:7b")
        data = config.model_dump()
        assert data["backend"] == "ollama"
        restored = ModelConfig.model_validate(data)
        assert restored.backend == ModelBackend.OLLAMA
