"""Local context never follows credential paths or blocks on special files."""
import os
import subprocess
import sys

import pytest

from local_coder.memory.context_store import ContextMemory


@pytest.mark.parametrize("relative", [".env", ".aws/config", ".ssh/config", ".codex/config.toml", ".docker/config.json",
                                      "client.PEM", "client.P12", "client.PFX", "my.keystore", "secrets.toml", "credentials.dev"])
def test_private_sources_are_excluded_from_index_and_explicit_memory(tmp_path, relative):
    source = tmp_path / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("synthetic_private_fixture_only")
    memory = ContextMemory(tmp_path)
    assert memory.index_code()["files"] == 0
    with pytest.raises(ValueError, match="Private files"):
        memory.ingest("tool", "observed private source", relative)
    assert memory.status()["memories"] == 0


def test_source_links_are_not_silently_resolved_to_other_project_files(tmp_path):
    source = tmp_path / "source.py"
    source.write_text("def parser():\n    return 1\n")
    linked = tmp_path / "linked.py"
    linked.symlink_to(source)
    memory = ContextMemory(tmp_path)
    with pytest.raises((OSError, ValueError)):
        memory.ingest("code", source.read_text(), "linked.py")
    assert memory.index_code(["linked.py"])["files"] == 0


def test_new_source_symlink_cannot_verify_saved_memory(tmp_path):
    source = tmp_path / "source.py"
    source.write_text("def parser():\n    return 1\n")
    memory = ContextMemory(tmp_path)
    memory.index_code()
    saved = memory.search("parser")[0]
    replacement = tmp_path / "replacement.py"
    source.rename(replacement)
    source.symlink_to(replacement)
    assert memory.search("parser") == []
    assert memory.get(saved["id"])["freshness"] == "source_changed"


def test_external_link_replacement_does_not_abort_unrelated_index_update(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    source = root / "source.py"
    source.write_text("def parser():\n    return 1\n")
    memory = ContextMemory(root)
    memory.index_code()
    source.unlink()
    source.symlink_to(tmp_path / "outside.py")
    (root / "other.py").write_text("def other():\n    return 2\n")
    assert memory.index_code(["other.py"])["updated"] == 1
    assert memory.search("parser") == []
    assert memory.search("other")[0]["source_path"] == "other.py"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX special files only")
def test_indexing_fifo_returns_without_waiting_for_a_writer(tmp_path):
    os.mkfifo(tmp_path / "pipe.py")
    # A subprocess bound prevents a regressed blocking reader from hanging pytest.
    code = "from local_coder.memory.context_store import ContextMemory; import sys; print(ContextMemory(sys.argv[1]).index_code()['skipped'])"
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"


def test_source_growth_or_preparation_failure_retains_old_index(tmp_path, monkeypatch):
    source = tmp_path / "source.py"
    source.write_text("def original():\n    return 1\n")
    memory = ContextMemory(tmp_path)
    memory.index_code()
    original = memory.search("original")[0]
    source.write_text("def replacement():\n    return 2\n")

    def failed_preparation(*args, **kwargs):
        raise ValueError("local embedding unavailable")

    monkeypatch.setattr(memory, "_prepare", failed_preparation)
    result = memory.index_code()
    assert result["files"] == 0 and result["updated"] == 0 and result["skipped"] == 1
    assert memory.get(original["id"]) is not None
    assert memory.get(original["id"])["freshness"] == "source_changed"
    assert memory.search("original") == []


def test_candidate_cap_applies_globally_across_roots_and_large_directories(tmp_path, monkeypatch):
    for name in ("one", "two"):
        directory = tmp_path / name
        directory.mkdir()
        for number in range(8):
            (directory / f"{number}.pem").write_text("excluded fixture")
    memory = ContextMemory(tmp_path, max_index_files=2)
    indexable = memory._indexable
    candidates = []

    def counted(path, root):
        candidates.append(path)
        return indexable(path, root)

    monkeypatch.setattr(memory, "_indexable", counted)
    result = memory.index_code(["one", "two"])
    assert len(candidates) == 10
    assert result["files"] == 0 and result["truncated"] is True


def test_partial_index_reports_limit_without_deleting_unvisited_sources(tmp_path):
    for number in range(3):
        (tmp_path / f"{number}.py").write_text(f"def parser_{number}():\n    return {number}\n")
    full = ContextMemory(tmp_path)
    full.index_code()
    bounded = ContextMemory(tmp_path, max_index_files=1)
    result = bounded.index_code()
    assert result["files"] == 1 and result["truncated"] is True
    assert len(full.list_memories()) == 3
