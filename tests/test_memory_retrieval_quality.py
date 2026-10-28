"""Fixed local labels and state transitions for retrieval quality and provenance."""
import json
import sqlite3

import pytest

from local_coder.memory.context_store import ContextMemory
from local_coder.memory_api import MemoryAuditAPI


@pytest.mark.parametrize("query,label", [
    ("parse_config", "parser"),
    ("rotate_logs", "logging"),
    ("validate_invoice", "billing"),
])
def test_fixed_labeled_symbol_queries_have_correct_top_result(tmp_path, query, label):
    # Deliberately fixed labels, independent of the ranker's implementation.
    sources = {
        "parser": "def parse_config(raw):\n    return raw.strip()\n",
        "logging": "def rotate_logs(path):\n    return path\n",
        "billing": "def validate_invoice(invoice):\n    return bool(invoice)\n",
    }
    for name, source in sources.items():
        (tmp_path / f"{name}.py").write_text(source)
    memory = ContextMemory(tmp_path)
    memory.index_code()
    memory.ingest("conversation", "Discuss parser logging billing operations generally")
    result = memory.search(query, limit=1)
    assert result[0]["source_path"] == f"{label}.py"
    assert result[0]["freshness"] == "fresh"
    audit = memory.retrieval(result[0]["retrieval_id"])
    assert audit["candidates"][0]["selected"]
    assert audit["candidates"][0]["score"] > 0
    assert "exact AST symbol" in audit["candidates"][0]["reasons"]


def test_content_freshness_across_branch_edit_delete_and_task_state(tmp_path, monkeypatch):
    source = tmp_path / "parser.py"
    source.write_text("def parse_config():\n    return 1\n")
    memory = ContextMemory(tmp_path)
    monkeypatch.setattr(memory, "_head", lambda: "branch-a-head")
    memory.index_code()
    observation = memory.ingest("tool", "parse_config observation", "parser.py")[0]
    memory.task_state("task", {"objective": "Repair parse_config", "constraints": ["Keep API stable"]})
    monkeypatch.setattr(memory, "_head", lambda: "branch-b-head")
    assert {row["kind"] for row in memory.search("parse_config")} == {"code", "tool"}
    assert all(row["source_status"] == "content_verified" for row in memory.search("parse_config"))
    source.write_text("def replacement():\n    return 2\n")
    assert memory.search("parse_config") == []
    audit = memory.retrievals(1)[0]
    assert all(row["exclusion"] == "stale observation: source_changed" for row in audit["candidates"])
    assert observation["id"] in {row["id"] for row in audit["candidates"]}
    source.unlink()
    assert memory.search("parse_config") == []
    assert all(row["freshness"] == "source_missing" for row in memory.retrievals(1)[0]["candidates"])
    assert memory.task_state("task")["constraints"] == ["Keep API stable"]


def test_branch_bound_observations_excluded_but_intent_retained(tmp_path, monkeypatch):
    memory = ContextMemory(tmp_path)
    monkeypatch.setattr(memory, "_head", lambda: "old")
    observation = memory.ingest("evidence", "parser result was successful")[0]
    decision = memory.ingest("decision", "parser must remain compatible")[0]
    monkeypatch.setattr(memory, "_head", lambda: "new")
    hits = memory.search("parser")
    assert [row["id"] for row in hits] == [decision["id"]]
    excluded = next(row for row in memory.retrievals(1)[0]["candidates"] if row["id"] == observation["id"])
    assert excluded["exclusion"] == "stale observation: head_changed"


def test_correction_delete_and_task_filter_quality(tmp_path):
    memory = ContextMemory(tmp_path)
    target = memory.ingest("finding", "obsolete_vulnerability", task_id="active")[0]
    memory.ingest("finding", "replacement_evidence", task_id="other")
    memory.update(target["id"], "replacement_evidence")
    assert memory.search("obsolete_vulnerability") == []
    assert [row["id"] for row in memory.search("replacement_evidence", task_id="active")] == [target["id"]]
    memory.delete(target["id"])
    assert memory.search("replacement_evidence", task_id="active") == []


def test_audit_is_bounded_redacted_and_records_context_delivery(tmp_path):
    memory = ContextMemory(tmp_path)
    secret = "sk-" + "a" * 40
    content = "parser private irrelevant body " * 30
    memory.ingest("decision", content)
    result = memory.context("parser token=" + secret, char_budget=100)
    audit = memory.retrieval(result["retrieval_id"])
    assert secret not in json.dumps(audit)
    assert content not in json.dumps(audit)
    assert audit["rendered_characters"] == len(result["text"]) <= 100
    assert audit["used_characters"] == sum(len(row["content"]) for row in result["items"])
    assert {row["id"] for row in audit["candidates"] if row["selected"]} == {row["id"] for row in result["items"]}
    for _ in range(102):
        memory.search("", char_budget=0)
    assert len(memory.retrievals(1000)) == 100
    with sqlite3.connect(memory.db_path) as connection:
        assert secret not in "\n".join(connection.iterdump())


def test_audit_caps_candidates_and_explains_result_limit(tmp_path):
    memory = ContextMemory(tmp_path)
    for number in range(135):
        memory.ingest("conversation", f"parser evidence {number}")
    memory.search("parser", limit=1, char_budget=50)
    audit = memory.retrievals(1)[0]
    assert len(audit["candidates"]) == 128
    assert audit["candidate_count"] == 135
    assert audit["omitted_candidates"] == 7
    assert sum(row["selected"] for row in audit["candidates"]) == 1
    assert all(row.get("exclusion") for row in audit["candidates"] if not row["selected"])


def test_audit_api_validation_and_storage_guard(tmp_path):
    class Inspector(MemoryAuditAPI):
        context_memory = ContextMemory(tmp_path)
    api = Inspector()
    api.context_memory.search("parser")
    handled, body = api._memory_audit_get("/memory/retrievals", {"limit": ["1"]})
    assert handled and len(body["retrievals"]) == 1
    audit_id = body["retrievals"][0]["id"]
    assert api._memory_audit_get("/memory/retrievals/" + audit_id, {})[1]["retrieval"]["id"] == audit_id
    with pytest.raises(ValueError):
        api._memory_audit_get("/memory/retrievals/../outside", {})
    with pytest.raises(ValueError):
        api._memory_audit_get("/memory/retrievals", {"unexpected": ["1"]})
    api.context_memory.db_path.unlink()
    api.context_memory.db_path.symlink_to(tmp_path / "outside.sqlite")
    with pytest.raises(ValueError, match="links"):
        api.context_memory.retrievals()


@pytest.mark.parametrize('kind', ['code', 'finding', 'tool', 'decision'])
def test_source_bound_correction_is_retrievable_without_source_verification(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(ContextMemory, '_head', lambda self: 'fixed-head')
    source = tmp_path / 'parser.py'
    source.write_text('def parse_config():\n    return 1\n')
    memory = ContextMemory(tmp_path)
    original = memory.ingest(kind, source.read_text(), 'parser.py')[0]
    corrected = memory.update(original['id'], 'human correction for parse_config')
    assert corrected['source_hash'] == original['source_hash']
    assert corrected['freshness'] == 'corrected_unverified'
    assert corrected['source_status'] == 'corrected_unverified'
    hits = memory.search('human correction')
    assert [row['id'] for row in hits] == [original['id']]
    assert 'human-corrected memory; source does not verify this text' in hits[0]['reasons']
    audit = memory.retrieval(hits[0]['retrieval_id'])
    assert audit['candidates'][0]['source_status'] == 'corrected_unverified'
    if kind == 'code':
        assert memory.structure()['items'] == []
        assert not any(key in corrected['metadata'] for key in ('anchor', 'calls', 'qualified_name'))
    source.write_text('def replacement():\n    return 2\n')
    assert memory.get(original['id'])['freshness'] == 'source_changed'
    if kind != 'decision':
        assert memory.search('human correction') == []
