from local_coder.context.compression import compress_messages
from local_coder.orchestrator.sessions import SessionStore
from local_coder.remote import RemoteControlServer
from local_coder.types import Message


def test_context_compression_preserves_task_and_recent_history():
    messages = [
        Message(role="system", content="system context"),
        Message(role="user", content="the task"),
        Message(role="tool", content="old output " * 30),
        Message(role="assistant", content="new output"),
    ]

    compacted = compress_messages(messages, 80)

    assert compacted[0].content == "system context"
    assert compacted[1].content == "the task"
    assert "compacted" in compacted[2].content
    assert compacted[-1].content.endswith("new output")


def test_session_store_persists_and_lists_sessions(tmp_path):
    store = SessionStore(str(tmp_path))
    saved = store.save("s1", request="add tests", phase="plan", result="Plan created")

    assert store.get("s1")["request"] == "add tests"
    assert store.list()[0]["session_id"] == saved["session_id"]


def test_remote_control_exposes_local_status_and_rejects_unknown_routes(tmp_path):
    server = RemoteControlServer(str(tmp_path))

    status, payload = server.handle("GET", "/status")
    missing_status, missing = server.handle("GET", "/missing")

    assert status == 200
    assert payload["project_root"] == str(tmp_path)
    assert missing_status == 404
    assert missing["error"] == "not found"