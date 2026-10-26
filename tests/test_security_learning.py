"""Tests for the self-improving security review: lessons, triage, the
findings ledger, compaction pinning and batched reviews."""
import asyncio
import json
from types import SimpleNamespace

from local_coder.agents import SecurityAgent, create_agent
from local_coder.agents.base import PINNED_PREFIX
from local_coder.orchestrator.coordinator import Coordinator
from local_coder.security.batching import collect_files, plan_batches
from local_coder.security.ledger import FindingsLedger, LedgerEntry
from local_coder.security.lessons import LESSONS_FILE, Lesson, LessonStore, parse_lessons
from local_coder.security.triage import BASELINE_FILE, LAST_SCAN_FILE, dedupe, promote_last_scan
from local_coder.tools import create_tool_registry
from local_coder.tools.security import Finding, SecurityScanTool
from local_coder.types import AgentRole, AgentTask, ModelResponse, ProjectConfig, ToolCall, ToolName

AWS_KEY = "AKIA" + "Q" * 16


def run(coro):
    return asyncio.run(coro)


# --- lessons ---------------------------------------------------------------

LESSONS = """# Security lessons

Hand-written intro that must survive edits.

## Suppress

- secrets/hardcoded-credential | tests/** | Fixtures use fake credentials.

## Confirmed

- B602 | src/jobs.py | shell=True with the job name.

## Patterns

- Every handler calls auth.require_user.
"""


def test_parse_lessons_and_match():
    lessons = parse_lessons(LESSONS)

    assert [(lesson.kind, lesson.rule, lesson.path) for lesson in lessons] == [
        ("suppress", "secrets/hardcoded-credential", "tests/**"),
        ("confirmed", "B602", "src/jobs.py"),
        ("pattern", "", ""),
    ]
    suppress = lessons[0]
    assert suppress.matches("secrets", "hardcoded-credential", "tests/unit/test_db.py")
    assert not suppress.matches("secrets", "hardcoded-credential", "src/db.py")
    assert not suppress.matches("secrets", "aws-access-key", "tests/unit/test_db.py")
    assert Lesson("suppress", "x", "", "docs/*").matches("bandit", "B101", "docs/conf.py")


def test_proposals_wait_for_review_then_append_to_file(tmp_path):
    (tmp_path / LESSONS_FILE).write_text(LESSONS)
    store = LessonStore(tmp_path)
    fp = Lesson("suppress", "Uses a constant command.", "B603", "src/build.py")
    duplicate = Lesson("confirmed", "shell=True with the job name.", "B602", "src/jobs.py")

    queued = store.propose([fp, duplicate, fp])

    assert queued == [fp]  # already-accepted and repeated lessons are skipped
    assert fp not in store.load()  # nothing reaches the file without review
    assert [lesson.id for lesson in store.pending()] == [fp.id]

    assert store.accept([fp.id]) == [fp]
    text = (tmp_path / LESSONS_FILE).read_text()
    assert "Hand-written intro that must survive edits." in text
    suppress_section = text.split("## Suppress")[1].split("## Confirmed")[0]
    assert "- B603 | src/build.py | Uses a constant command." in suppress_section
    assert store.pending() == []


def test_reject_and_create_file_from_scratch(tmp_path):
    store = LessonStore(tmp_path)
    keep, drop = Lesson("pattern", "Templates autoescape."), Lesson("pattern", "Noise.")
    store.propose([keep, drop])

    assert store.reject([drop.id]) == [drop]
    store.accept()

    assert parse_lessons((tmp_path / LESSONS_FILE).read_text()) == [keep]
    assert "Templates autoescape." in store.render_for_prompt()


# --- triage in security_scan ----------------------------------------------

def test_scan_applies_suppressions_and_marks_new(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "conftest.py").write_text('DB_PASSWORD = "fixture-pass-123"\n')
    (tmp_path / "settings.py").write_text(f'AWS = "{AWS_KEY}"\n')
    (tmp_path / LESSONS_FILE).write_text(LESSONS)
    tool = SecurityScanTool(str(tmp_path))

    first = run(tool.execute(scanners=["secrets"]))
    assert "settings.py:1" in first.output
    assert "conftest.py" not in first.output
    assert f"Suppressed by {LESSONS_FILE}: 1" in first.output
    assert "[new]" not in first.output  # no baseline yet

    assert promote_last_scan(tmp_path)
    # Code shifts down a line: the old finding keeps its identity.
    (tmp_path / "settings.py").write_text(f'# config\nAWS = "{AWS_KEY}"\nTOKEN = "{"ghp_" + "b2" * 18}"\n')
    second = run(tool.execute(scanners=["secrets"]))

    assert "New since the last review: 1" in second.output
    assert "settings.py:3 [HIGH] secrets/github-token" in second.output and "github-token: GitHub token" in second.output
    new_lines = [line for line in second.output.splitlines() if line.endswith("[new]")]
    assert len(new_lines) == 1 and "settings.py:3" in new_lines[0]
    assert json.loads((tmp_path / BASELINE_FILE).read_text())


def test_dedupe_merges_scanners_on_one_line():
    findings = [
        Finding("app.py", 4, "MEDIUM", "bandit", "B602", "shell"),
        Finding("app.py", 4, "HIGH", "semgrep", "cmd-injection", "shell"),
        Finding("app.py", 9, "LOW", "bandit", "B101", "assert"),
    ]
    merged = dedupe(findings)

    assert len(merged) == 2
    assert merged[0].severity == "HIGH" and merged[0].scanner == "bandit+semgrep"


# --- ledger and record_finding --------------------------------------------

def test_record_finding_tool_upserts_and_stays_in_workspace(tmp_path):
    registry = create_tool_registry(str(tmp_path))
    args = {"title": "SQL injection in get_user", "severity": "HIGH", "path": "app/db.py", "line": 12,
            "exploit_path": "?id= flows into cursor.execute via f-string", "confidence": 6}

    first = run(registry.execute_tool(AgentRole.SECURITY, "record_finding", args))
    second = run(registry.execute_tool(AgentRole.SECURITY, "record_finding", {
        **args, "status": "confirmed", "confidence": 9, "fix": "Use a parameterized query.", "exploit_path": "",
    }))
    third = run(registry.execute_tool(AgentRole.SECURITY, "record_finding", {
        "title": "SQL injection in get_user", "severity": "HIGH", "path": "app/db.py", "line": 12,
    }))
    escape = run(registry.execute_tool(AgentRole.SECURITY, "record_finding", {**args, "path": "../../etc/passwd"}))
    denied = run(registry.execute_tool(AgentRole.CODER, "record_finding", args))

    assert first.success and "Recorded F1" in first.output
    assert second.success and "Updated F1" in second.output
    assert third.success and "conf 9/10 confirmed" in third.output  # a bare update doesn't downgrade
    assert not escape.success and "escapes workspace" in escape.output
    assert not denied.success
    [entry] = FindingsLedger.for_project(tmp_path).entries
    assert (entry.status, entry.confidence, entry.fix) == ("confirmed", 9, "Use a parameterized query.")
    assert entry.exploit_path.startswith("?id=")  # empty update keeps the recorded detail


def test_ledger_render_keeps_most_severe_within_budget(tmp_path):
    ledger = FindingsLedger(tmp_path / "ledger.json")
    for i in range(30):
        ledger.record(LedgerEntry(f"Low issue {i}", "LOW", f"src/m{i}.py", i + 1, exploit_path="x" * 200))
    ledger.record(LedgerEntry("RCE via pickle", "CRITICAL", "src/api.py", 7, confidence=9,
                              exploit_path="request body -> pickle.loads", fix="Use json."))

    text = ledger.render(max_chars=800)

    assert len(text) <= 800
    assert text.splitlines()[0].startswith("F31 [CRITICAL conf 9/10 suspected] src/api.py:7 RCE via pickle")
    assert "lower-priority findings omitted" in text


def test_ledger_proposes_lessons_and_filters_low_confidence(tmp_path):
    ledger = FindingsLedger(tmp_path / "ledger.json")
    ledger.record(LedgerEntry("Hard-coded key", "HIGH", "tests/fixtures.py", 3, rule="secrets/aws-access-key",
                              status="false_positive", lesson="Fixture keys are fake."))
    ledger.record(LedgerEntry("Path traversal", "HIGH", "app/files.py", 20, status="confirmed", confidence=10,
                              fix="Resolve and check the path is under the upload dir.", lesson="Uploads go through safe_join."))
    ledger.record(LedgerEntry("Maybe SSRF", "MEDIUM", "app/fetch.py", 5, confidence=5))

    assert [e.title for e in ledger.reportable()] == ["Path traversal"]
    kinds = [(lesson.kind, lesson.rule, lesson.path) for lesson in ledger.proposed_lessons()]
    assert kinds == [
        ("suppress", "secrets/aws-access-key", "tests/fixtures.py"),
        ("confirmed", "", "app/files.py"),
        ("pattern", "", ""),
    ]


# --- compaction keeps the ledger ------------------------------------------

class ScriptedModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.config = SimpleNamespace(temperature=0.0, max_tokens=100)

    async def generate(self, messages, **kwargs):
        self.calls.append(list(messages))
        return next(self.responses)


def test_security_agent_repins_ledger_after_compaction(tmp_path):
    (tmp_path / "big.py").write_text("x = 1\n" * 3000)
    registry = create_tool_registry(str(tmp_path))
    record = ToolCall(name="record_finding", arguments={
        "title": "Command injection", "severity": "HIGH", "path": "big.py", "line": 2,
        "exploit_path": "argv[1] -> os.system", "fix": "Use subprocess with a list", "confidence": 9,
    })
    model = ScriptedModel([
        ModelResponse(content="", tool_calls=[record]),
        ModelResponse(content="", tool_calls=[ToolCall(name="read_file", arguments={"path": "big.py"})]),
        ModelResponse(content="", tool_calls=[ToolCall(name="read_file", arguments={"path": "big.py", "start_line": 1})]),
        ModelResponse(content="Report"),
    ])
    agent = create_agent(AgentRole.SECURITY, model, registry, context_window_chars=6000, compact_context_chars=3000)

    response = run(agent.execute(AgentTask(role=AgentRole.SECURITY, objective="Review")))

    assert response.summary == "Report"
    compacted = [call for call in model.calls if any(m.content.startswith(PINNED_PREFIX) for m in call)]
    assert compacted, "compaction should have happened and pinned the ledger"
    pinned = [m for m in compacted[-1] if m.content.startswith(PINNED_PREFIX)]
    assert len(pinned) == 1
    assert "big.py:2 Command injection" in pinned[0].content and "argv[1] -> os.system" in pinned[0].content


def test_security_lessons_reach_the_prompt(tmp_path):
    agent = create_agent(AgentRole.SECURITY, ScriptedModel([]), create_tool_registry(str(tmp_path)))
    task = AgentTask(role=AgentRole.SECURITY, objective="Review")
    task.context.security_lessons = "### Known false positives (do not report these)\n- B101 | tests/** | asserts"

    assert isinstance(agent, SecurityAgent)
    assert "B101 | tests/** | asserts" in agent._format_task(task)
    assert "record_finding" in agent.system_prompt
    registry = create_tool_registry(str(tmp_path))
    assert registry.has_permission(AgentRole.SECURITY, ToolName.RECORD_FINDING)
    assert registry.has_permission(AgentRole.EXPLOIT_VALIDATOR, ToolName.RECORD_FINDING)
    assert not registry.has_permission(AgentRole.SECURITY, ToolName.WRITE_FILE)


# --- batching and the full review -----------------------------------------

def test_plan_batches_puts_risky_files_first(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "util.py").write_text("def add(a, b):\n    return a + b\n" * 40)
    (tmp_path / "src" / "api.py").write_text("import subprocess\n@app.route('/x')\ndef x():\n    subprocess.run(request.args['c'], shell=True)\n" * 10)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("eval(x)")
    (tmp_path / "README.md").write_text("docs")

    files = collect_files(tmp_path)
    batches = plan_batches(tmp_path, files, batch_chars=1000)

    assert sorted(files) == ["src/api.py", "src/util.py"]
    assert batches == [["src/api.py"], ["src/util.py"]]


def test_batched_review_carries_ledger_and_proposes_lessons(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "run.py").write_text("import os\nos.system(input())\n" * 30)
    (tmp_path / "app" / "fixtures.py").write_text("KEY = 1\n" * 200)
    coordinator = Coordinator(config=ProjectConfig(project_root=str(tmp_path)), project_root=str(tmp_path))
    models = [
        ScriptedModel([
            ModelResponse(content="", tool_calls=[ToolCall(name="record_finding", arguments={
                "title": "Command injection", "severity": "CRITICAL", "path": "app/run.py", "line": 2,
                "status": "confirmed", "confidence": 10, "exploit_path": "stdin -> os.system",
                "fix": "Do not pass input to a shell.",
            })]),
            ModelResponse(content="Batch 1 done."),
        ]),
        ScriptedModel([
            ModelResponse(content="", tool_calls=[ToolCall(name="record_finding", arguments={
                "title": "Hard-coded key", "severity": "LOW", "path": "app/fixtures.py", "line": 1,
                "rule": "secrets/hardcoded-credential", "status": "false_positive", "lesson": "Fixture values are fake.",
            })]),
            ModelResponse(content="Batch 2 done."),
        ]),
    ]
    queue = iter(models)
    coordinator.model_manager.get_model = lambda role: asyncio.sleep(0, result=next(queue))
    (tmp_path / LAST_SCAN_FILE).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / LAST_SCAN_FILE).write_text(json.dumps(["stale"]))

    report = run(coordinator.security_review(batch_chars=800))

    second_task = next(m.content for m in models[1].calls[0] if m.role == "user")
    assert "batch 2 of 2" in second_task and "app/run.py:2" in second_task  # ledger handed over
    assert "### Critical: Command injection" in report and "stdin -> os.system" in report
    assert "Dismissed as false positives" in report and "Hard-coded key" in report
    assert "Proposed lessons (2)" in report
    assert {lesson.kind for lesson in LessonStore(tmp_path).pending()} == {"suppress", "confirmed"}
    assert not (tmp_path / LESSONS_FILE).exists()  # proposals only, until a person accepts
    assert not (tmp_path / BASELINE_FILE).exists()  # stale scan state was cleared at the start
