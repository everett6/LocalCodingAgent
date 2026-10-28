"""Working-state updates coexist with retained review evidence after merging."""
import asyncio
from types import SimpleNamespace

import pytest

from local_coder.agents.base import BaseAgent, PINNED_PREFIX
from local_coder.context.working_memory import WORKING_MEMORY_PREFIX
from local_coder.tools import create_tool_registry
from local_coder.types import AgentRole, AgentTask, Message, ModelResponse, TaskStatus


class ReviewAgent(BaseAgent):
    role = AgentRole.SECURITY
    system_prompt = "Review only the authorized local source."


@pytest.mark.parametrize("refresh_working_memory", [True, False])
def test_runtime_context_updates_ledger_without_losing_working_memory(tmp_path, refresh_working_memory):
    ledger = tmp_path / ".local-coder/security/ledger.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text('[{"title": "first observed finding"}]\n')
    registry = create_tool_registry(str(tmp_path))
    model = SimpleNamespace(config=SimpleNamespace(model_id="test"))
    agent = ReviewAgent(model, registry)
    task = AgentTask(role=AgentRole.SECURITY, objective="Review parser")
    initial = [Message(role="system", content=agent.system_prompt),
               Message(role="user", content=task.objective)]
    first = asyncio.run(agent._runtime_context(initial, task))
    saved_working_memory = next(m for m in first if m.content.startswith(WORKING_MEMORY_PREFIX))
    updated_ledger = '[{"title": "verified local finding", "line": 7}]\n'
    ledger.write_text(updated_ledger)
    agent._files_changed.add("parser.py")

    second = asyncio.run(agent._runtime_context(first, task, refresh_working_memory=refresh_working_memory))

    pins = [m for m in second if m.content.startswith(PINNED_PREFIX)]
    working_states = [m for m in second if m.content.startswith(WORKING_MEMORY_PREFIX)]
    assert len(pins) == len(working_states) == 1
    assert pins[0].content.endswith(updated_ledger)
    assert "first observed finding" not in pins[0].content
    assert pins[0].role == working_states[0].role == "user"
    assert second[0] == initial[0]
    if refresh_working_memory:
        assert "parser.py" in working_states[0].content
    else:
        assert working_states[0] == saved_working_memory


def test_explicit_refusal_retains_original_while_updating_goal_status():
    class Ledger:
        def __init__(self):
            self.changes = []
            self.finished = None

        def create(self, *args):
            return {"id": args[-1]}

        def update(self, identity, changes):
            self.changes.append((identity, changes))

        def get(self, identity):
            return {"id": identity}

        def finish(self, response, **kwargs):
            self.finished = response

    class Registry:
        goal_ledger = Ledger()

        def get_schemas_for_role(self, role):
            return []

    class Model:
        config = SimpleNamespace(temperature=0.1, max_tokens=100)

        async def generate(self, messages, **kwargs):
            return ModelResponse(content="I cannot fulfill this request.")

    registry = Registry()
    task = AgentTask(role=AgentRole.SECURITY, objective="Review source")
    result = asyncio.run(ReviewAgent(Model(), registry).execute(task))

    assert result.status == TaskStatus.BLOCKED
    assert result.summary == "I cannot fulfill this request."
    assert registry.goal_ledger.finished is result
    assert registry.goal_ledger.changes == [(task.task_id, {"status": "running"})]
