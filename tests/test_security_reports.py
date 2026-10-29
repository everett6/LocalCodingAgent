import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from local_coder.security_reports import (
    MAX_RECORD_BYTES,
    EvidenceStore,
    export_sarif,
    findings_report,
    import_sarif,
    reproducibility_summary,
)


def finding(**changes):
    return dict(
        title="SQL injection",
        path="src/app.py",
        line=8,
        severity="HIGH",
        rule="sql_injection",
        description="Input reaches SQL",
        exploit_path="Local regression case",
        fix="Bind parameters",
        **changes,
    )


def test_evidence_chain_is_private_redacted_and_anchored(tmp_path):
    store = EvidenceStore(tmp_path)
    first = store.append("finding", {"api_key": "do-not-persist", "finding": finding()})
    second = store.append("note", {"output": "Bearer abcdefghijklmnopqrstuvwxyz"})
    assert second["previous_hash"] == first["hash"]
    assert store.verify(expected_head=second["hash"])["valid"]
    assert store.read() == [first, second]
    text = store.path.read_text()
    assert "do-not-persist" not in text and "abcdefghijklmnopqrstuvwxyz" not in text
    if __import__("os").name != "nt":
        assert store.path.stat().st_mode & 0o777 == 0o600
        assert store.path.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("change", ["data", "reorder", "tail", "malformed"])
def test_chain_detects_edits_and_truncation_with_anchor(tmp_path, change):
    store = EvidenceStore(tmp_path)
    store.append("note", {"message": "one"})
    last = store.append("note", {"message": "two"})
    lines = store.path.read_text().splitlines()
    if change == "data":
        row = json.loads(lines[0])
        row["data"]["message"] = "changed"
        lines[0] = json.dumps(row)
    elif change == "reorder":
        lines.reverse()
    elif change == "tail":
        lines.pop()
    else:
        lines[0] = "{broken"
    store.path.write_text("\n".join(lines) + "\n")
    assert not store.verify(expected_head=last["hash"])["valid"]
    if change != "tail":
        with pytest.raises(ValueError):
            store.append("note", {})


def test_concurrent_stores_preserve_all_records(tmp_path):
    def append(number):
        return EvidenceStore(tmp_path).append("note", {"number": number})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append, range(24)))
    store = EvidenceStore(tmp_path)
    assert store.verify()["records"] == 24
    assert {row["data"]["number"] for row in store.read()} == set(range(24))


def test_processes_share_lock_across_atomic_replacements(tmp_path):
    code = """import sys
from local_coder.security_reports import EvidenceStore
for number in range(5):
    EvidenceStore(sys.argv[1]).append('note', {'worker': sys.argv[2], 'number': number})
"""
    children = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(tmp_path), str(i)],
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for i in range(3)
    ]
    for child in children:
        output = child.communicate(timeout=20)
        assert child.returncode == 0, output
    assert EvidenceStore(tmp_path).verify()["records"] == 15


def test_lock_contention_is_bounded(tmp_path, monkeypatch):
    @contextmanager
    def busy(*args, **kwargs):
        raise BlockingIOError("Busy")
        yield

    monkeypatch.setattr("local_coder.security_reports.exclusive_lock", busy)
    monkeypatch.setattr("local_coder.security_reports.LOCK_TIMEOUT_SECONDS", 0.001)
    with pytest.raises(ValueError, match="busy"):
        EvidenceStore(tmp_path).append("note", {})


def test_secret_copies_are_redacted_in_evidence_and_reports(tmp_path):
    raw = {
        "password": "synthetic-private-value",
        "output": "Observed synthetic-private-value",
    }
    record = EvidenceStore(tmp_path).append("note", raw)
    assert "synthetic-private-value" not in json.dumps(record)
    row = finding()
    row.update(
        api_key="synthetic-private-value", title="Observed synthetic-private-value"
    )
    assert "synthetic-private-value" not in findings_report([row])


def test_evidence_rejects_symlink_hardlink_and_oversize(tmp_path):
    store = EvidenceStore(tmp_path)
    store.append("note", {})
    with pytest.raises(ValueError):
        store.append("note", {"large": "x" * MAX_RECORD_BYTES})
    other = tmp_path / "other"
    other.hardlink_to(store.path)
    with pytest.raises(ValueError):
        store.append("note", {})
    other.unlink()
    store.path.unlink()
    try:
        store.path.symlink_to(other)
    except OSError:
        pytest.skip("Symlinks unavailable")
    with pytest.raises(ValueError):
        store.read()


def test_remediation_requires_same_recorded_finding_and_case(tmp_path):
    store = EvidenceStore(tmp_path)
    case = "a" * 64
    before = store.append(
        "verification_attempt",
        {"finding_id": "f1", "case_hash": case, "outcome": "triggered"},
    )
    after = store.append(
        "verification_attempt",
        {"finding_id": "f1", "case_hash": case, "outcome": "not_triggered"},
    )
    record = store.record_remediation("f1", before["hash"], after["hash"])
    assert record["data"]["case_hash"] == case
    assert record["data"]["verification"] == "reported"
    with pytest.raises(ValueError):
        store.record_remediation("f2", before["hash"], after["hash"])
    mismatch = store.append(
        "verification_attempt",
        {"finding_id": "f1", "case_hash": "b" * 64, "outcome": "not_triggered"},
    )
    with pytest.raises(ValueError):
        store.record_remediation("f1", before["hash"], mismatch["hash"])
    with pytest.raises(ValueError):
        store.append("remediation_verification", {"finding_id": "f1"})


def test_sarif_export_and_import_preserve_findings_without_trusting_verification():
    source = finding(verification="verified", cwe="CWE-89")
    report = export_sarif([source])
    assert report["version"] == "2.1.0"
    run = report["runs"][0]
    assert run["tool"]["driver"]["name"] == "local-coder"
    assert run["tool"]["driver"]["rules"][0]["properties"]["tags"] == ["CWE-89"]
    result = run["results"][0]
    assert result["level"] == "error"
    assert result["locations"][0]["physicalLocation"]["region"]["startLine"] == 8
    assert "security-severity" not in json.dumps(report)
    imported = import_sarif(report)
    assert len(imported) == 1
    assert imported[0]["path"] == "src/app.py" and imported[0]["cwe"] == "CWE-89"
    assert imported[0]["verification"] == "unverified"
    assert imported[0]["status"] == "suspected"


@pytest.mark.parametrize(
    "path",
    [
        "../x",
        "/etc/passwd",
        "C:/x",
        "file:///x",
        "https://example.com/x",
        "a/../../x",
        "%252e%252e/x",
        "a\\x",
    ],
)
def test_sarif_rejects_escaping_and_remote_paths(path):
    report = export_sarif([finding()])
    report["runs"][0]["results"][0]["locations"][0]["physicalLocation"][
        "artifactLocation"
    ]["uri"] = path
    with pytest.raises(ValueError):
        import_sarif(report)


def test_sarif_bounded_external_refs_and_rule_index():
    report = export_sarif([finding()])
    report["runs"][0]["externalPropertyFileReferences"] = {
        "results": [{"location": {"uri": "https://example.com/data"}}]
    }
    with pytest.raises(ValueError):
        import_sarif(report)
    with pytest.raises(ValueError):
        import_sarif('{"version":"2.1.0","version":"other","runs":[]}')
    with pytest.raises(ValueError):
        import_sarif(" " * 4_000_001)
    report = export_sarif([finding()])
    result = report["runs"][0]["results"][0]
    result.pop("ruleId")
    assert import_sarif(report)[0]["rule"] == "sql_injection"
    result["ruleIndex"] = 999
    with pytest.raises(ValueError):
        import_sarif(report)


def test_markdown_escapes_active_markup_redacts_and_does_not_invent_scores():
    row = finding()
    row.update(
        title="<script>bad</script> [click](https://evil.test)",
        fix="password='private-password'",
        verification="unverified",
    )
    report = findings_report([row])
    assert "<script>" not in report and "[click](" not in report
    assert "private-password" not in report
    assert "unverified" in report and "CWE-89" in report
    assert "CVSS" not in report


def test_advanced_audit_nested_evidence_and_cwe_rule_tags():
    report = export_sarif(
        [
            {
                "rule_id": "custom-sql",
                "CWE": "CWE-89",
                "severity": "MEDIUM",
                "reasons": ["Local source reaches sink"],
                "evidence": {
                    "file": "app.py",
                    "line": 9,
                    "source_trace": ["input", "execute"],
                },
            }
        ]
    )
    result = report["runs"][0]["results"][0]
    assert result["message"]["text"] == "Local source reaches sink"
    assert result["locations"][0]["physicalLocation"]["region"]["startLine"] == 9
    result["properties"].pop("cwe")
    assert import_sarif(report)[0]["cwe"] == "CWE-89"


def test_sarif_rule_id_without_index_uses_rule_metadata_and_default_level():
    report = export_sarif([finding()])
    run = report["runs"][0]
    result = run["results"][0]
    result.pop("ruleIndex")
    result.pop("level")
    result.pop("properties")
    run["tool"]["driver"]["rules"][0]["defaultConfiguration"] = {"level": "error"}
    result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] = (
        "./src/app.py"
    )
    row = import_sarif(report)[0]
    assert row["cwe"] == "CWE-89" and row["severity"] == "HIGH"
    assert row["path"] == "src/app.py"


def test_imported_info_severity_round_trips_but_verification_does_not():
    row = finding()
    row.update(severity="INFO", verification="verified")
    imported = import_sarif(export_sarif([row]))[0]
    assert imported["severity"] == "INFO"
    assert imported["verification"] == "unverified"


@pytest.mark.parametrize(
    "status, phrase",
    [
        ("changed", "Historical observation: source content changed"),
        ("missing", "Historical observation: the recorded source is now missing"),
        (
            "unavailable",
            "Historical observation: current source content could not be checked",
        ),
        ("unchanged", "Current source content matches the recorded hash"),
    ],
)
def test_exports_preserve_source_and_record_origin_with_freshness_qualification(
    status, phrase
):
    row = finding()
    row.update(
        evidence={"source_sha256": "a" * 64},
        audit_record_hash="b" * 64,
        audit_recorded_at="2026-10-25T12:30:00+00:00",
        source_status=status,
    )
    properties = export_sarif([row])["runs"][0]["results"][0]["properties"]
    assert properties["source_sha256"] == "a" * 64
    assert properties["audit_record_hash"] == "b" * 64
    assert properties["audit_recorded_at"] == row["audit_recorded_at"]
    assert properties["source_status"] == status
    assert phrase in properties["source_qualification"]
    markdown = findings_report([row])
    assert "a" * 64 in markdown and "b" * 64 in markdown
    assert phrase.replace(":", "\\:") in markdown


def test_import_cannot_claim_local_audit_origin_or_current_source_match():
    row = finding()
    row.update(
        source_sha256="a" * 64,
        audit_record_hash="b" * 64,
        audit_recorded_at="2026-10-25T12:30:00+00:00",
        source_status="unchanged",
    )
    imported = import_sarif(export_sarif([row]))[0]
    assert imported["source_sha256"] == "a" * 64
    assert imported["source_status"] == "unavailable"
    assert imported["audit_record_hash"] is None
    assert imported["audit_recorded_at"] is None
    assert imported["verification"] == "unverified"


@pytest.mark.parametrize(
    "metadata",
    [
        {"source_sha256": "unverified"},
        {"audit_record_hash": "wrong"},
        {"audit_recorded_at": "2026-10-25T12:00:00"},
        {"source_status": "unchanged"},
        {"source_status": "verified"},
    ],
)
def test_invalid_source_origin_metadata_is_rejected(metadata):
    row = finding()
    row.update(metadata)
    with pytest.raises(ValueError):
        export_sarif([row])


def test_atomic_replace_failure_preserves_existing_chain(tmp_path, monkeypatch):
    store = EvidenceStore(tmp_path)
    first = store.append("note", {})

    def fail(*args):
        raise OSError("Synthetic disk failure")

    monkeypatch.setattr("local_coder.security_reports.os.replace", fail)
    with pytest.raises(OSError):
        store.append("note", {"second": True})
    assert store.verify(expected_head=first["hash"])["valid"]
    assert list(store.path.parent.glob(".evidence-*")) == []


def test_evidence_allows_bounded_audit_report_and_does_not_claim_signature(tmp_path):
    store = EvidenceStore(tmp_path)
    row = store.append(
        "static_audit", {"findings": [{"description": "x" * 2000}] * 100}
    )
    verification = store.verify(expected_head=row["hash"])
    assert verification["algorithm"] == "sha256"
    assert "signature" not in verification


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {1: "bad"}])
def test_non_json_values_are_rejected_before_storage(tmp_path, value):
    with pytest.raises(ValueError):
        EvidenceStore(tmp_path).append("note", {"value": value})


def test_reproducibility_counts_only_compatible_evidence_and_distinct_attempts():
    rows = [
        {
            "id": str(i),
            "finding_id": "f1",
            "case_hash": "a" * 64,
            "evidence_hash": "b" * 64,
            "outcome": outcome,
            "verified": True,
        }
        for i, outcome in enumerate(["triggered", "triggered", "not_triggered"])
    ]
    rows += [rows[0], {"finding_id": "f1", "verified": True, "outcome": "triggered"}]
    summary = reproducibility_summary(rows)
    assert summary["verification"] == "reported"
    assert summary["reported_attempts"] == 5
    assert summary["eligible_attempts"] == 3
    assert summary["duplicate_attempts"] == 1
    assert summary["groups"][0]["triggered"] == 2
    assert summary["groups"][0]["reproducibility"] == pytest.approx(2 / 3)
    assert summary["verified_attempts"] == 0
