"""External memory privacy, freshness, persistence and retrieval contracts."""
import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from local_coder.memory.context_store import ContextMemory


def test_persistent_hybrid_memory_and_project_isolation(tmp_path):
    memory = ContextMemory(tmp_path / "one")
    row = memory.ingest("decision", "Use parseConfig to validate configuration", task_id="task-a")[0]
    hit = ContextMemory(tmp_path / "one").search("parse configuration")[0]
    assert hit["id"] == row["id"]
    assert hit["reasons"] and hit["source_ref"]
    assert hit["embedding_backend"] == "lexical-hash-v1"
    assert ContextMemory(tmp_path / "two").search("configuration") == []
    assert memory.status()["memories"] == 1


def test_redacts_ingress_metadata_corrections_and_task_state(tmp_path):
    memory = ContextMemory(tmp_path)
    secret = "sk-" + "a" * 40
    row = memory.ingest("conversation", f"token: {secret}", metadata={"api_key": "private-value"})[0]
    assert secret not in json.dumps(row)
    assert "private-value" not in json.dumps(row)
    memory.update(row["id"], "password = '" + "super-private-password" + "'")
    state = memory.task_state("task-a", {"objective": f"Investigate api_key={secret}"})
    assert secret not in json.dumps(state)
    assert "super-private-password" not in json.dumps(memory.get(row["id"]))
    assert memory.search(secret) == []
    with sqlite3.connect(memory.db_path) as connection:
        dump = "\n".join(connection.iterdump())
    assert secret not in dump and "super-private-password" not in dump


def test_code_freshness_and_long_lived_decision(tmp_path):
    path = tmp_path / "example.py"
    path.write_text("def parse_configuration():\n    return 1\n")
    memory = ContextMemory(tmp_path)
    memory.index_code()
    decision = memory.ingest("decision", "Configuration uses a strict parser", source_path="example.py")[0]
    assert any(hit["kind"] == "code" for hit in memory.search("parse_configuration"))
    path.write_text("def unrelated():\n    return 2\n")
    assert all(hit["kind"] != "code" for hit in memory.search("parse_configuration"))
    assert memory.get(decision["id"])["freshness"] == "source_changed"
    assert any(hit["id"] == decision["id"] for hit in memory.search("configuration"))
    memory.index_code()
    assert memory.search("unrelated")[0]["kind"] == "code"
    path.unlink()
    assert all(hit["kind"] != "code" for hit in memory.search("unrelated"))


def test_source_and_storage_paths_cannot_escape(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("sensitive = 1")
    memory = ContextMemory(root)
    for path in ("../outside.py", str(outside)):
        with pytest.raises(ValueError):
            memory.ingest("code", "sensitive", source_path=path)
        with pytest.raises(ValueError):
            memory.index_code([path])
    (root / "linked.py").symlink_to(outside)
    assert memory.index_code()["files"] == 0
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    (hostile / ".local-coder").symlink_to(tmp_path / "external")
    with pytest.raises(ValueError):
        ContextMemory(hostile)


def test_index_excludes_private_binary_and_state_files(tmp_path):
    (tmp_path / ".env").write_text("PASSWORD=supersecret")
    (tmp_path / "private.pem").write_text("private data")
    (tmp_path / "blob.py").write_bytes(b"abc\x00def")
    (tmp_path / "normal.py").write_text("def useful_parser():\n    return 1\n")
    memory = ContextMemory(tmp_path)
    summary = memory.index_code()
    assert summary["files"] == 1
    assert all(row["source_path"] == "normal.py" for row in memory.list_memories())


def test_update_delete_rebuild_indices_and_graph_atomically(tmp_path):
    memory = ContextMemory(tmp_path)
    original = memory.ingest("finding", "alpha-vulnerability", task_id="t")[0]
    related = memory.ingest("decision", "Mitigate original finding", metadata={"links": [original["id"]]})[0]
    assert original["id"] in memory.get(related["id"])["links"]
    memory.update(original["id"], "omega-correction")
    assert memory.search("alpha-vulnerability") == []
    assert memory.search("omega-correction")[0]["id"] == original["id"]
    assert memory.delete(original["id"]) is True
    assert memory.get(original["id"]) is None
    assert memory.search("omega-correction") == []
    assert original["id"] not in memory.get(related["id"])["links"]
    with sqlite3.connect(memory.db_path) as connection:
        assert connection.execute("SELECT count(*) FROM vectors WHERE memory_id=?", (original["id"],)).fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM terms WHERE memory_id=?", (original["id"],)).fetchone()[0] == 0


def test_bounded_context_and_structured_state_are_separate(tmp_path):
    memory = ContextMemory(tmp_path)
    memory.ingest("tool", "parser evidence " * 1000, task_id="task-a")
    state = memory.task_state("task-a", {"objective": "Fix parsing", "failed_attempts": ["regex insufficient"], "unfinished_work": ["test unicode"]})
    assert state["failed_attempts"] == ["regex insufficient"]
    assert memory.task_state("task-a")["objective"] == "Fix parsing"
    for budget in (0, 1, 80, 200, 1024):
        result = memory.context("parser evidence", char_budget=budget, task_id="task-a")
        assert len(result["text"]) <= budget
        assert sum(len(hit["content"]) for hit in memory.search("parser", char_budget=budget)) <= budget
    assert memory.context("parser", char_budget=500)["untrusted"] is True
    with pytest.raises(ValueError):
        memory.task_state("task-a", {"arbitrary": "bad"})


def test_concurrent_instances_and_private_permissions(tmp_path):
    memory = ContextMemory(tmp_path)
    def write(n):
        return ContextMemory(tmp_path).ingest("evidence", f"parser evidence {n}")[0]["id"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(write, range(30)))
    assert len(set(ids)) == 30
    assert memory.status()["memories"] == 30
    assert memory.db_path.stat().st_mode & 0o077 == 0
    assert memory.db_path.parent.stat().st_mode & 0o077 == 0


def test_memory_tools(tmp_path):
    from local_coder.tools.memory import MemorySearchTool, MemoryWriteTool, TaskMemoryTool
    memory = ContextMemory(tmp_path)
    result = asyncio.run(MemoryWriteTool(memory).execute(kind="decision", content="Use parser contracts"))
    assert result.success
    search = asyncio.run(MemorySearchTool(memory).execute(query="parser", char_budget=180))
    assert search.success and search.data["items"]
    assert len(search.output) <= 180
    state = asyncio.run(TaskMemoryTool(memory).execute(task_id="task-a", updates={"objective": "Repair parser"}))
    assert state.success and state.data["objective"] == "Repair parser"


def test_redacts_metadata_keys_and_ignores_state_in_git(tmp_path):
    memory = ContextMemory(tmp_path)
    secret = "ghp_" + "a" * 36
    row = memory.ingest("decision", "privacy", metadata={secret: "unsafe metadata key"})[0]
    assert secret not in json.dumps(row)
    assert (tmp_path / ".local-coder" / ".gitignore").read_text().splitlines()[-1] == "*"


def test_head_change_invalidates_code_but_retains_decisions(tmp_path, monkeypatch):
    path = tmp_path / "example.py"
    path.write_text("def parse_configuration():\n    return 1\n")
    memory = ContextMemory(tmp_path)
    monkeypatch.setattr(memory, "_head", lambda: "first-head")
    memory.index_code()
    memory.ingest("decision", "Configuration stays strict")
    monkeypatch.setattr(memory, "_head", lambda: "second-head")
    hits = memory.search("configuration")
    assert hits and all(hit["kind"] == "decision" for hit in hits)
    assert hits[0]["freshness"] == "head_changed"


def test_correction_removes_old_graph_provenance(tmp_path):
    memory = ContextMemory(tmp_path)
    source = memory.ingest("finding", "Original evidence")[0]
    decision = memory.ingest("decision", "Initial conclusion", metadata={"links": [source["id"]]})[0]
    memory.update(decision["id"], "Corrected unrelated conclusion")
    assert memory.get(decision["id"])["links"] == []


def test_explicit_local_backend_and_invalid_vector(tmp_path):
    class LocalBackend:
        name = "user-supplied-local-test"
        def __call__(self, text):
            return [1.0, 0.0] if "feline" in text or "cat" in text else [0.0, 1.0]
    memory = ContextMemory(tmp_path, embedding_backend=LocalBackend())
    row = memory.ingest("decision", "feline behavior")[0]
    assert memory.search("cat")[0]["id"] == row["id"]
    assert memory.status()["embedding_backend"] == LocalBackend.name
    with pytest.raises(ValueError):
        ContextMemory(tmp_path, embedding_backend=lambda text: [1.0])


def test_task_filtered_retrieval_and_python_symbols(tmp_path):
    memory = ContextMemory(tmp_path)
    memory.ingest("decision", "parser for another task", task_id="other")
    memory.ingest("decision", "parser shared project context")
    memory.ingest("decision", "parser current task", task_id="current")
    assert len(memory.search("parser", task_id="current")) == 2
    (tmp_path / "symbols.py").write_text("import os\n\ndef first_symbol():\n    return 1\n\ndef second_symbol():\n    return 2\n")
    memory.index_code(["symbols.py"])
    hit = memory.search("second_symbol")[0]
    assert hit["source_ref"] == "symbols.py:6"
    assert "first_symbol" not in hit["content"]


def test_index_hash_describes_the_content_actually_read(tmp_path, monkeypatch):
    path = tmp_path / "race.py"
    path.write_text("def original_symbol():\n    return 1\n")
    memory = ContextMemory(tmp_path)
    prepare = memory._prepare
    def change_before_prepare(*args, **kwargs):
        path.write_text("def replacement_symbol():\n    return 2\n")
        return prepare(*args, **kwargs)
    monkeypatch.setattr(memory, "_prepare", change_before_prepare)
    memory.index_code(["race.py"])
    assert memory.search("original_symbol") == []


def test_redaction_before_chunking_and_backend_failure_is_atomic(tmp_path):
    class FailingBackend:
        name = "explicit-local-backend"
        def __call__(self, text):
            raise ValueError("embedding unavailable")
    memory = ContextMemory(tmp_path)
    secret = "sk-" + "b" * 40
    memory.ingest("conversation", "x" * 1780 + " api_key=" + secret)
    assert secret not in json.dumps(memory.list_memories())
    failure = ContextMemory(tmp_path, embedding_backend=FailingBackend())
    before = memory.status()["memories"]
    with pytest.raises(ValueError, match="unavailable"):
        failure.ingest("decision", "Do not partially write this")
    assert memory.status()["memories"] == before
