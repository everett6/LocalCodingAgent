import pytest

from local_coder.orchestrator.sessions import SessionError, SessionStore


def test_progress_combines_saved_phases_plan_and_latest_task_status(tmp_path):
    session = SessionStore(str(tmp_path)).create("tracked")
    turn = session.start_turn("implement a parser")
    checkpoint = session.checkpoint(turn)
    checkpoint.put("explore", "inspected parser")
    checkpoint.put("plan", {
        "objective": "implement a parser",
        "tasks": [
            {"task_id": "parse", "role": "coder", "objective": "Implement parsing"},
            {"task_id": "tests", "role": "tester", "objective": "Test malformed input"},
        ],
    })
    session.record_event({
        "event_type": "task_status",
        "data": {"task_id": "parse", "status": "running", "note": ""},
    })
    session.record_event({
        "event_type": "task_status",
        "data": {"task_id": "parse", "status": "completed", "note": "parser added"},
    })

    progress = session.progress()

    assert progress["session_id"] == "tracked"
    assert progress["request"] == "implement a parser"
    assert progress["current_phase"] == "plan"
    assert progress["completed_phases"] == ["explore", "plan"]
    assert [task["status"] for task in progress["tasks"]] == ["completed", "pending"]
    assert progress["task_counts"] == {"pending": 1, "running": 0, "completed": 1, "failed": 0}
    assert progress["tasks"][0]["summary"] == "parser added"


def test_progress_rejects_missing_turns(tmp_path):
    session = SessionStore(str(tmp_path)).create()
    session.start_turn("first")
    session.start_turn("second")

    with pytest.raises(SessionError, match="Turn not found"):
        session.progress(9)
