"""Finding updates must survive stale reviewers and interrupted writes."""
import json
import os
import subprocess
import sys

import pytest

from local_coder.security.ledger import FindingsLedger, LedgerEntry


def finding(title, path="app.py", **kwargs):
    return LedgerEntry(title, "HIGH", path, **kwargs)


def test_stale_reviewers_merge_latest_state(tmp_path):
    first = FindingsLedger.for_project(tmp_path)
    second = FindingsLedger.for_project(tmp_path)
    first.record(finding("First", confidence=9, exploit_path="input to sink"))
    second.record(finding("Second"))
    first.record(finding("First", status="confirmed", confidence=10))
    entries = FindingsLedger.for_project(tmp_path).entries
    assert [entry.id for entry in entries] == ["F1", "F2"]
    assert entries[0].exploit_path == "input to sink"
    assert entries[0].status == "confirmed"


def test_process_writers_do_not_lose_findings(tmp_path):
    script = """
import sys
from local_coder.security.ledger import FindingsLedger, LedgerEntry
ledger = FindingsLedger.for_project(sys.argv[1])
for index in range(10):
    ledger.record(LedgerEntry(f'Issue {sys.argv[2]}-{index}', 'HIGH', 'app.py'))
"""
    processes = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path), str(i)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for i in range(6)]
    try:
        for process in processes:
            _, error = process.communicate(timeout=30)
            assert process.returncode == 0, error.decode()
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    entries = FindingsLedger.for_project(tmp_path).entries
    assert len(entries) == 60
    assert len({entry.id for entry in entries}) == 60
    assert len({entry.title for entry in entries}) == 60


def test_failed_replace_keeps_previous_snapshot(tmp_path, monkeypatch):
    ledger = FindingsLedger.for_project(tmp_path)
    ledger.record(finding("Original"))
    original = ledger.path.read_bytes()

    def fail(*args):
        raise OSError("simulated interrupted save")

    monkeypatch.setattr("local_coder.security.ledger.os.replace", fail)
    with pytest.raises(OSError, match="interrupted"):
        ledger.record(finding("New"))
    assert ledger.path.read_bytes() == original
    assert list(ledger.path.parent.glob(".ledger-*.tmp")) == []


@pytest.mark.parametrize("raw", ["{broken", "{}", "[null]", '[{"title":"missing fields"}]'])
def test_invalid_ledger_is_not_silently_overwritten(tmp_path, raw):
    ledger = FindingsLedger.for_project(tmp_path)
    ledger.path.write_text(raw)
    with pytest.raises(ValueError, match="ledger"):
        ledger.record(finding("New"))
    assert ledger.path.read_text() == raw


@pytest.mark.parametrize("target", ["directory", "ledger", "lock"])
def test_symlink_state_is_rejected(tmp_path, target):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("untouched")
    if target == "directory":
        (project / ".local-coder").symlink_to(outside, target_is_directory=True)
    else:
        state = project / ".local-coder" / "security"
        state.mkdir(parents=True)
        (state / ("ledger.json" if target == "ledger" else "ledger.json.lock")).symlink_to(sentinel)
    with pytest.raises(ValueError, match="symlink"):
        FindingsLedger.for_project(project)
    assert sentinel.read_text() == "untouched"


def test_json_format_and_private_permissions_preserved(tmp_path):
    ledger = FindingsLedger.for_project(tmp_path)
    ledger.record(finding("Original"))
    assert json.loads(ledger.path.read_text())[0]["id"] == "F1"
    if os.name != "nt":
        assert ledger.path.stat().st_mode & 0o077 == 0
