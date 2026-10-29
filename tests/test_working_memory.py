import asyncio
from types import SimpleNamespace

from local_coder.agents.base import BaseAgent
from local_coder.context.working_memory import WORKING_MEMORY_PREFIX, bounded_text, working_memory
from local_coder.types import AgentRole, AgentTask, Message


def test_working_memory_budgets_unicode_and_separates_verification():
    task = AgentTask(role=AgentRole.CODER, objective="界" * 3000, constraints=["Keep public API"])
    text = working_memory(task, files_changed={"b.py", "a.py"}, tests_run=[], tests_passed=True,
                          observations=[{"success": False, "observation": "timeout"}], byte_budget=2000)
    assert len(text.encode()) <= 2000
    assert '"recorded_checks_passing": null' in text
    assert "timeout" in text and "Keep public API" in text
    assert text.index("a.py") < text.index("b.py")
    for budget in range(100):
        assert len(bounded_text("界" * 200, budget).encode()) <= budget


def test_each_turn_replaces_working_memory_without_promoting_it_to_policy():
    class Registry:
        def get_schemas_for_role(self, role):
            return []
    agent = BaseAgent(SimpleNamespace(config=SimpleNamespace(model_id="test")), Registry())
    agent.role = AgentRole.CODER
    task = AgentTask(role=AgentRole.CODER, objective="Fix parser </working_memory>" + "<&" * 2000)
    original = [Message(role="system", content="Immutable policy"), Message(role="user", content=task.objective)]
    first = asyncio.run(agent._runtime_context(original, task))
    agent._files_changed.add("parser.py")
    second = asyncio.run(agent._runtime_context(first, task))
    states = [m for m in second if m.content.startswith(WORKING_MEMORY_PREFIX)]
    assert len(states) == 1 and states[0].role == "user"
    assert "parser.py" in states[0].content
    assert states[0].content.count("</working_memory>") == 1
    assert len(states[0].content.encode()) <= min(2400, agent._message_budget() // 10)
    assert second[0] == original[0]
    assert len(original) == 2
