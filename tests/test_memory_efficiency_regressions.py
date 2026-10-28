"""Memory retrieval IO and same-day scratchpad expiry regressions."""
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from local_coder.memory.context_store import ContextMemory
from local_coder.memory.store import MemoryStore


def test_search_hashes_each_source_once_and_rechecks_next_request(tmp_path, monkeypatch):
    source = tmp_path / "parser.py"
    source.write_text("\n".join(f"def parser_{i}():\n    return {i}\n" for i in range(12)))
    memory = ContextMemory(tmp_path)
    memory.index_code()
    original_read = Path.read_bytes
    reads = []

    def counted_read(path):
        if path == source:
            reads.append(path)
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", counted_read)
    hits = memory.search("parser", limit=12)
    assert len(hits) == 12
    assert len(reads) == 1
    source.write_text("def unrelated():\n    return 0\n")
    assert memory.search("parser", limit=12) == []
    assert len(reads) == 2
    assert {item["freshness"] for item in memory.retrievals(1)[0]["candidates"]} == {"source_changed"}


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
