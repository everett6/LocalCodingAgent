"""Tests for concise verification failure extraction."""
from local_coder.verification.failures import parse_failures, summarize_failures


def test_parse_pytest_failures():
    output = """============================= test session starts =============================
FAILED tests/test_auth.py::test_expired - AssertionError: expected 401
FAILED tests/test_auth.py::test_missing - E   ValueError: missing token
=========================== short test summary info ============================
"""

    failures = parse_failures(output)

    assert [failure.test for failure in failures] == [
        "tests/test_auth.py::test_expired",
        "tests/test_auth.py::test_missing",
    ]
    assert "expected 401" in summarize_failures(output)
    assert "missing token" in summarize_failures(output)


def test_failure_summary_falls_back_to_recent_output():
    output = "runner crashed\n" + ("x" * 5000)

    summary = summarize_failures(output)

    assert len(summary) == 4000
    assert summary.endswith("x" * 4000)