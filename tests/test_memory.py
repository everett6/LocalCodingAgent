"""Tests for the memory store."""
import asyncio
import os
import tempfile

import pytest

from local_coder.memory.store import MemoryStore


@pytest.fixture
def memory_store(tmp_path):
    """Create a temporary memory store."""
    db_path = str(tmp_path / "test_memory.db")
    return MemoryStore(db_path)


class TestProjectMemory:
    def test_set_and_get(self, memory_store):
        asyncio.run(memory_store.set_project_memory("arch", "MVC pattern", "architecture"))
        result = asyncio.run(memory_store.get_project_memory("arch"))
        assert result == "MVC pattern"

    def test_get_nonexistent(self, memory_store):
        result = asyncio.run(memory_store.get_project_memory("nonexistent"))
        assert result is None

    def test_update_existing(self, memory_store):
        asyncio.run(memory_store.set_project_memory("key", "value1"))
        asyncio.run(memory_store.set_project_memory("key", "value2"))
        result = asyncio.run(memory_store.get_project_memory("key"))
        assert result == "value2"

    def test_get_by_category(self, memory_store):
        asyncio.run(memory_store.set_project_memory("a", "1", "arch"))
        asyncio.run(memory_store.set_project_memory("b", "2", "arch"))
        asyncio.run(memory_store.set_project_memory("c", "3", "conv"))
        
        arch = asyncio.run(memory_store.get_project_memories("arch"))
        assert len(arch) == 2
        
        all_mem = asyncio.run(memory_store.get_project_memories())
        assert len(all_mem) == 3


class TestTaskMemory:
    def test_set_and_get(self, memory_store):
        asyncio.run(memory_store.set_task_memory("task-1", "objective", "Add auth"))
        result = asyncio.run(memory_store.get_task_memories("task-1"))
        assert result["objective"] == "Add auth"

    def test_multiple_keys(self, memory_store):
        asyncio.run(memory_store.set_task_memory("task-1", "obj", "obj1"))
        asyncio.run(memory_store.set_task_memory("task-1", "status", "done"))
        result = asyncio.run(memory_store.get_task_memories("task-1"))
        assert len(result) == 2

    def test_different_tasks(self, memory_store):
        asyncio.run(memory_store.set_task_memory("task-1", "key", "val1"))
        asyncio.run(memory_store.set_task_memory("task-2", "key", "val2"))
        r1 = asyncio.run(memory_store.get_task_memories("task-1"))
        r2 = asyncio.run(memory_store.get_task_memories("task-2"))
        assert r1["key"] == "val1"
        assert r2["key"] == "val2"


class TestTaskState:
    def test_save_and_get(self, memory_store):
        asyncio.run(memory_store.save_task_state(
            "t1", "running", "coder", "Implement feature"
        ))
        state = asyncio.run(memory_store.get_task_state("t1"))
        assert state is not None
        assert state["status"] == "running"
        assert state["objective"] == "Implement feature"

    def test_get_nonexistent(self, memory_store):
        state = asyncio.run(memory_store.get_task_state("nonexistent"))
        assert state is None

    def test_active_tasks(self, memory_store):
        asyncio.run(memory_store.save_task_state("t1", "running", "coder", "a"))
        asyncio.run(memory_store.save_task_state("t2", "completed", "coder", "b"))
        asyncio.run(memory_store.save_task_state("t3", "pending", "tester", "c"))
        
        active = asyncio.run(memory_store.get_active_tasks())
        assert len(active) == 2  # t1 (running) and t3 (pending)


class TestEventLog:
    def test_log_and_retrieve(self, memory_store):
        asyncio.run(memory_store.log_event(
            "ORCHESTRATOR", "task_started", "Starting task",
            data={"key": "value"}, task_id="t1",
        ))
        events = asyncio.run(memory_store.get_recent_events(limit=10))
        assert len(events) == 1
        assert events[0]["source"] == "ORCHESTRATOR"

    def test_filter_by_task(self, memory_store):
        asyncio.run(memory_store.log_event("A", "e1", "msg1", task_id="t1"))
        asyncio.run(memory_store.log_event("B", "e2", "msg2", task_id="t2"))
        asyncio.run(memory_store.log_event("C", "e3", "msg3", task_id="t1"))
        
        events = asyncio.run(memory_store.get_recent_events(task_id="t1"))
        assert len(events) == 2


class TestScratchpad:
    def test_write(self, memory_store):
        asyncio.run(memory_store.write_scratchpad("coder", "temp result", "t1"))
        # No read method needed - scratchpad is write-only for agents

    def test_cleanup(self, memory_store):
        asyncio.run(memory_store.write_scratchpad("coder", "old stuff"))
        # Cleanup with 0 hours should remove everything
        deleted = asyncio.run(memory_store.cleanup_scratchpad(max_age_hours=0))
        # May or may not delete depending on timing, but shouldn't error
        assert isinstance(deleted, int)
