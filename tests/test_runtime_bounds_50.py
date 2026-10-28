"""Runtime regressions for bounded capture, fresh tests and catalog snapshots."""
import asyncio
import json
import os
import py_compile
import sys
from pathlib import Path

import pytest

from local_coder.core.event_bus import AgentEvent, InMemoryEventBus, WebSocketClient, WebSocketEventBus
from local_coder.processes import communicate, process_options
from local_coder.providers import catalog
from local_coder.verification.test_runner import format_report, run_tests


def test_capture_streams_without_communicate_and_keeps_draining():
    async def scenario():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c",
            "import os; [(os.write(1,b'x'*8192),os.write(2,b'y'*8192)) for _ in range(256)]",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **process_options(),
        )
        async def forbidden():
            raise AssertionError("unbounded communicate used")
        proc.communicate = forbidden
        stdout, stderr = await communicate(proc, timeout=10, max_stream_bytes=4096, max_total_bytes=6000)
        assert proc.returncode == 0
        assert len(stdout) <= 4096 and len(stderr) <= 4096
        assert len(stdout) + len(stderr) <= 6000
        assert b"truncated" in stdout and b"truncated" in stderr
        assert stdout.endswith(b"x" * 100) and stderr.endswith(b"y" * 100)
    asyncio.run(scenario())


def test_capture_tail_preserves_utf8_and_closes_child_stdin():
    async def scenario():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import sys; sys.stdin.read(); print('🙂'*1000)",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **process_options(),
        )
        stdout, stderr = await communicate(proc, timeout=3, max_stream_bytes=199)
        assert "truncated" in stdout.decode("utf-8")
        assert stdout.decode("utf-8").endswith("🙂\n")
        assert stderr == b"" and proc.returncode == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_streaming_capture_reaps_on_timeout_and_cancellation(cancel):
    async def scenario():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import os; exec('while True: os.write(1,b\"x\"*8192)')",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **process_options(),
        )
        task = asyncio.create_task(communicate(proc, timeout=.15, max_stream_bytes=4096))
        if cancel:
            await asyncio.sleep(.05)
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else asyncio.TimeoutError):
            await asyncio.wait_for(task, 3)
        assert proc.returncode is not None
    asyncio.run(scenario())


@pytest.mark.parametrize("framework", ["unittest", "pytest"])
def test_runner_ignores_same_second_same_size_existing_bytecode(tmp_path, framework):
    source = tmp_path / "subject.py"
    source.write_text("VALUE = 1\n")
    stamp = 1700000000
    os.utime(source, (stamp, stamp))
    bytecode = py_compile.compile(str(source), doraise=True)
    original_bytecode = Path(bytecode).read_bytes()
    source.write_text("VALUE = 2\n")
    os.utime(source, (stamp, stamp))
    (tmp_path / "test_fresh.py").write_text(
        "import unittest\nfrom subject import VALUE\nclass Fresh(unittest.TestCase):\n"
        " def test_fresh(self): self.assertEqual(VALUE, 2)\n"
    )
    report = asyncio.run(run_tests(tmp_path, framework=framework))
    assert report.ok, report.raw_output
    assert report.passed == 1
    assert Path(bytecode).read_bytes() == original_bytecode


def test_runner_labels_bounded_noisy_test_output(tmp_path):
    (tmp_path / "test_noisy.py").write_text(
        "import unittest\nclass Noise(unittest.TestCase):\n"
        " def test_noise(self): print('x'*1000000)\n"
    )
    report = asyncio.run(run_tests(tmp_path, framework="unittest"))
    assert report.ok and report.passed == 1
    assert len(report.raw_output) <= 200000
    assert "truncated" in report.raw_output
    assert "truncated" in format_report(report)


def test_event_histories_bound_task_count_and_return_copies():
    async def scenario():
        bus = InMemoryEventBus(max_task_events=3, max_tasks=2, max_all_events=5)
        for index in range(10):
            await bus.emit(AgentEvent("test", "progress", str(index), task_id="one"))
        recent = await bus.get_recent("one")
        assert [event.message for event in recent] == ["7", "8", "9"]
        recent.clear()
        assert len(await bus.get_recent("one")) == 3
        assert await bus.get_recent("one", 0) == []
        assert await bus.get_recent("one", -1) == []
        for task in ("two", "three"):
            await bus.emit(AgentEvent("test", "progress", "new", task_id=task))
        assert len(bus._task_events) == 2 and len(bus._all_events) == 5
        assert await bus.get_recent("one") == []
    asyncio.run(scenario())


def test_websocket_overflow_is_bounded_and_explicit():
    class Socket:
        closed = False
        async def close(self, *, code, reason):
            self.closed = True
            self.close_code = code
    async def scenario():
        socket = Socket()
        client = WebSocketClient(socket, max_queue_size=2, max_message_bytes=100)
        await client.send("one")
        await client.send("two")
        with pytest.raises(BufferError, match="overflow"):
            await client.send("three")
        assert client.closed()
        assert socket.closed and socket.close_code == 1013
        assert client._send_queue.qsize() <= 2
        oversized = WebSocketClient(Socket(), max_message_bytes=3)
        with pytest.raises(BufferError):
            await oversized.send("xxxx")
    asyncio.run(scenario())


def test_websocket_broadcast_drops_overflowed_client_and_keeps_healthy_one():
    class Socket:
        closed = False
        async def close(self, *, code, reason):
            self.closed = True
    async def scenario():
        bus = WebSocketEventBus()
        slow = WebSocketClient(Socket(), max_queue_size=1)
        healthy = WebSocketClient(Socket(), max_queue_size=4)
        bus.register_ws(slow)
        bus.register_ws(healthy)
        await bus.emit(AgentEvent("test", "progress", "one"))
        await bus.emit(AgentEvent("test", "progress", "two"))
        assert slow not in bus._ws_clients
        assert healthy in bus._ws_clients
        assert healthy._send_queue.qsize() == 2
    asyncio.run(scenario())


def test_catalog_snapshot_reuses_normalization_but_tracks_identity_and_ttl(tmp_path, monkeypatch):
    now = [1000.0]
    service = catalog.CatalogService(tmp_path, ttl_seconds=10, clock=lambda: now[0])
    target = service._cache_path()
    def payload(value):
        return json.dumps({"version": 1, "fetched_at": "1970-01-01T00:16:40+00:00", "data": [{
            "id": "test/model", "name": value, "context_length": 100,
            "pricing": {"prompt": "0", "completion": "0"},
        }]})
    target.write_text(payload("first"))
    calls = []
    normalize = catalog._catalog
    def counted(value):
        calls.append(1)
        return normalize(value)
    monkeypatch.setattr(catalog, "_catalog", counted)
    first = service.cached_model("test/model")
    first["supported_parameters"].append("bad")
    assert service.cached_model("test/model")["supported_parameters"] == []
    assert len(calls) == 1
    now[0] += 11
    assert service.cached_model("test/model")["stale"]
    previous = target.stat()
    replacement = target.with_suffix(".replacement")
    replacement.write_text(payload("other"))
    os.utime(replacement, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    replacement.replace(target)
    assert service.cached_model("test/model")["name"] == "other"
    assert len(calls) == 2
    target.write_text(payload("third"))
    os.utime(target, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    assert service.cached_model("test/model")["name"] == "third"
    now[0] = 1000.0
    public = service.openrouter_models()
    public["models"][0]["name"] = "mutated"
    assert service.cached_model("test/model")["name"] == "third"
    target.write_text("malformed")
    assert service.cached_model("test/model") is None
    target.write_text(payload("third"))
    assert service.cached_model("test/model")["name"] == "third"
    target.unlink()
    assert service.cached_model("test/model") is None
