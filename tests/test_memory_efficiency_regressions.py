"""Memory retrieval IO and same-day scratchpad expiry regressions."""
import asyncio
from datetime import datetime, timedelta

import pytest

import local_coder.memory.context_store as context_store

from local_coder.memory.context_store import ContextMemory
from local_coder.memory.store import MemoryStore


def test_search_hashes_each_source_once_and_rechecks_next_request(tmp_path, monkeypatch):
    source = tmp_path / "parser.py"
    source.write_text("\n".join(f"def parser_{i}():\n    return {i}\n" for i in range(12)))
    memory = ContextMemory(tmp_path)
    memory.index_code()
    original_read = context_store.read_source_snapshot
    reads = []

    def counted_read(root, path, **kwargs):
        if path == source:
            reads.append(path)
        return original_read(root, path, **kwargs)

    monkeypatch.setattr(context_store, "read_source_snapshot", counted_read)
    hits = memory.search("parser", limit=12)
    assert len(hits) == 12
    assert len(reads) == 1
    source.write_text("def unrelated():\n    return 0\n")
    assert memory.search("parser", limit=12) == []
    assert len(reads) == 2
    assert {item["freshness"] for item in memory.retrievals(1)[0]["candidates"]} == {"source_changed"}


@pytest.mark.parametrize("method", ["list_memories", "structure"])
def test_inspection_reuses_each_source_snapshot_but_never_across_requests(tmp_path, monkeypatch, method):
    source = tmp_path / "parser.py"
    source.write_text("\n".join(f"def parser_{i}():\n    return {i}\n" for i in range(12)))
    memory = ContextMemory(tmp_path)
    memory.index_code()
    original_read = context_store.read_source_snapshot
    reads = []

    def counted_read(root, path, **kwargs):
        reads.append(path)
        return original_read(root, path, **kwargs)

    monkeypatch.setattr(context_store, "read_source_snapshot", counted_read)
    result = getattr(memory, method)()
    assert len(result if method == "list_memories" else result["items"]) == 12
    assert reads == [source]
    source.write_text("def replacement():\n    return 0\n")
    result = getattr(memory, method)()
    assert reads == [source, source]
    if method == "list_memories":
        assert {row["freshness"] for row in result} == {"source_changed"}
    else:
        assert result["items"] == []


def test_ingest_checks_source_once_for_preparation_and_once_for_returned_freshness(tmp_path, monkeypatch):
    source = tmp_path / "notes.txt"
    source.write_text("source notes")
    memory = ContextMemory(tmp_path)
    original_read = context_store.read_source_snapshot
    reads = []

    def counted_read(root, path, **kwargs):
        reads.append(path)
        return original_read(root, path, **kwargs)

    monkeypatch.setattr(context_store, "read_source_snapshot", counted_read)
    rows = memory.ingest("conversation", "parser note\n" * 2000, "notes.txt")
    assert len(rows) > 10
    assert len(reads) == 2
    assert {row["freshness"] for row in rows} == {"fresh"}


def test_unchanged_index_does_not_rebuild_static_call_graph(tmp_path, monkeypatch):
    source = tmp_path / "parser.py"
    source.write_text("def helper():\n    return 1\n\ndef parser():\n    return helper()\n")
    memory = ContextMemory(tmp_path)
    memory.index_code()
    edges = memory.status()["code_call_edges"]
    assert edges > 0

    def unexpected_rebuild(connection):
        pytest.fail("An unchanged source index must retain its static call graph")

    monkeypatch.setattr(memory, "_refresh_code_graph", unexpected_rebuild)
    result = memory.index_code()
    assert result["updated"] == 0 and result["unchanged"] == 1
    assert memory.status()["code_call_edges"] == edges


def test_index_uses_one_source_snapshot_and_one_revision_for_all_files(tmp_path, monkeypatch):
    for name in ("one.py", "two.py"):
        (tmp_path / name).write_text("def parser():\n    return 1\n")
    memory = ContextMemory(tmp_path)
    original_read = context_store.read_source_snapshot
    reads, heads = [], []

    def counted_read(root, path, **kwargs):
        reads.append(path)
        return original_read(root, path, **kwargs)

    def counted_head():
        heads.append(None)
        return None

    monkeypatch.setattr(context_store, "read_source_snapshot", counted_read)
    monkeypatch.setattr(memory, "_head", counted_head)
    assert memory.index_code()["files"] == 2
    assert sorted(reads) == [tmp_path / "one.py", tmp_path / "two.py"]
    assert len(heads) == 1


def test_shared_source_snapshot_compares_each_memories_own_hash(tmp_path):
    source = tmp_path / "parser.py"
    source.write_text("old parser source")
    memory = ContextMemory(tmp_path)
    old = memory.ingest("tool", "parser observation before edit", "parser.py")[0]
    source.write_text("new parser source")
    current = memory.ingest("tool", "parser observation after edit", "parser.py")[0]

    hits = memory.search("parser observation")
    assert [item["id"] for item in hits] == [current["id"]]
    candidates = {item["id"]: item for item in memory.retrieval(hits[0]["retrieval_id"])["candidates"]}
    assert candidates[old["id"]]["freshness"] == "source_changed"
    assert candidates[current["id"]]["freshness"] == "fresh"


def test_scratchpad_cleanup_expires_old_entries_on_the_same_day(tmp_path, monkeypatch):
    now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)

    class Clock:
        @staticmethod
        def now():
            return now

    memory = MemoryStore(str(tmp_path / "memory.sqlite"))
    with memory._get_conn() as connection:
        connection.executemany(
            "INSERT INTO agent_scratchpad(agent_role, content, created_at) VALUES (?,?,?)",
            [("coder", "expired", (now - timedelta(hours=2)).isoformat()),
             ("coder", "recent", (now - timedelta(minutes=30)).isoformat())],
        )
    monkeypatch.setattr("local_coder.memory.store.datetime", Clock)
    assert asyncio.run(memory.cleanup_scratchpad(max_age_hours=1)) == 1
    with memory._get_conn() as connection:
        assert [row[0] for row in connection.execute("SELECT content FROM agent_scratchpad")] == ["recent"]
