"""Mock-model benchmark for local-coding-agent's orchestration layer.

benchmarks/run_benchmark.py measures raw LLM inference against a live local
model server (e.g. vLLM/llama.cpp at http://localhost:8090). That server is
not always running in every environment this repo is developed in, and when
it isn't, that script just reports connection errors for every stage.

This script instead benchmarks the parts of the system that don't depend on
having a model server up: the Coordinator's control flow (agent dispatch,
event forwarding, DAG scheduling, the verification/fix loop), the
CommandPolicy safety classifier, and the speculative drafter's cache, using
an in-process FakeModel with zero/near-zero simulated latency. This is also
the surface where a real bug (an event-callback signature mismatch that
crashed every single agent invocation the Coordinator made) previously
made a full run impossible to complete at all -- so "it finishes and
reports plausible numbers" is itself part of what's being verified here.

Run with:
    PYTHONPATH=src python3 benchmarks/run_mock_benchmark.py
"""
import asyncio
import json
import random
import statistics
import time
from types import SimpleNamespace

from local_coder.orchestrator.coordinator import Coordinator
from local_coder.safety import CommandPolicy
from local_coder.speculative.drafter import SpeculativeDrafter
from local_coder.types import (
    AgentRole,
    AgentTask,
    ModelBackend,
    ModelConfig,
    ModelResponse,
    ProjectConfig,
)


class FakeModel:
    """In-process stand-in for a LocalModel; no network, no GPU."""

    def __init__(self, content: str = "Done.", latency_s: float = 0.0):
        self.content = content
        self.latency_s = latency_s
        self.calls = 0
        self.config = ModelConfig(
            name="fake",
            backend=ModelBackend.OPENAI_COMPAT,
            model_id="fake-model",
            base_url="http://unused",
        )

    async def generate(self, messages, **kwargs):
        self.calls += 1
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        return ModelResponse(content=self.content, prompt_tokens=32, completion_tokens=16)


def make_coordinator(project_root: str, model_latency_s: float = 0.0, max_parallel_agents: int = 1) -> Coordinator:
    config = ProjectConfig(project_root=project_root)
    config.agentic.max_parallel_agents = max_parallel_agents
    coordinator = Coordinator(config=config, project_root=project_root)
    coordinator.model_manager.get_model = lambda role, model_name=None: asyncio.sleep(
        0, result=FakeModel(latency_s=model_latency_s)
    )
    return coordinator


async def benchmark_e2e_run(project_root: str, iterations: int = 10) -> dict:
    """Full Coordinator.run() with a fake model that completes every agent
    call on its first turn (no tool calls). Measures pure orchestration
    overhead: agent construction, message building, event dispatch,
    the review/test/fix pipeline."""
    latencies_ms = []
    for _ in range(iterations):
        coordinator = make_coordinator(project_root)
        events = []
        coordinator.on_event(events.append)

        start = time.perf_counter()
        report = await coordinator.run("Add a docstring to a math helper")
        elapsed_ms = (time.perf_counter() - start) * 1000
        latencies_ms.append(elapsed_ms)

        assert "Final Report" in report
        assert any(e.event_type == "task_completed" for e in events)

    return {
        "iterations": iterations,
        "mean_ms": round(statistics.mean(latencies_ms), 3),
        "p50_ms": round(statistics.median(latencies_ms), 3),
        "min_ms": round(min(latencies_ms), 3),
        "max_ms": round(max(latencies_ms), 3),
    }


async def benchmark_dag_scheduling(project_root: str, task_count: int = 25) -> dict:
    """A multi-task plan runs through the real DAG scheduler (validate +
    get_ready_tasks + mark_* + stall detection) instead of the single-task
    fallback path."""
    coordinator = make_coordinator(project_root)
    from local_coder.types import TaskPlan

    tasks = []
    for i in range(task_count):
        depends_on = [f"t{i-1}"] if i > 0 and i % 3 != 0 else []
        tasks.append(AgentTask(
            task_id=f"t{i}", role=AgentRole.CODER, objective=f"step {i}", depends_on=depends_on,
        ))
    plan = TaskPlan(objective="scheduling benchmark", tasks=tasks)

    start = time.perf_counter()
    result = await coordinator._execute_plan(plan)
    elapsed_ms = (time.perf_counter() - start) * 1000

    return {
        "task_count": task_count,
        "status": result.status.value,
        "total_ms": round(elapsed_ms, 3),
        "ms_per_task": round(elapsed_ms / task_count, 3),
    }


async def benchmark_parallel_speedup(project_root: str, task_count: int = 6, task_latency_s: float = 0.05) -> dict:
    """Independent (no depends_on) tasks should run concurrently when
    agentic.max_parallel_agents > 1. Compares wall-clock time for the same
    plan run sequentially (max_parallel_agents=1, today's default) vs. with
    all tasks able to run at once."""
    from local_coder.types import TaskPlan

    def make_plan() -> "TaskPlan":
        return TaskPlan(
            objective="parallel speedup benchmark",
            tasks=[
                AgentTask(task_id=f"p{i}", role=AgentRole.CODER, objective=f"independent step {i}")
                for i in range(task_count)
            ],
        )

    sequential_coordinator = make_coordinator(project_root, model_latency_s=task_latency_s, max_parallel_agents=1)
    start = time.perf_counter()
    await sequential_coordinator._execute_plan(make_plan())
    sequential_ms = (time.perf_counter() - start) * 1000

    parallel_coordinator = make_coordinator(project_root, model_latency_s=task_latency_s, max_parallel_agents=task_count)
    start = time.perf_counter()
    await parallel_coordinator._execute_plan(make_plan())
    parallel_ms = (time.perf_counter() - start) * 1000

    return {
        "task_count": task_count,
        "simulated_task_latency_ms": round(task_latency_s * 1000, 1),
        "sequential_ms": round(sequential_ms, 3),
        "parallel_ms": round(parallel_ms, 3),
        "speedup_x": round(sequential_ms / parallel_ms, 2) if parallel_ms else None,
    }


async def benchmark_malformed_plan_fail_fast(project_root: str) -> dict:
    """Confirms the stall-guard fix: a plan with a dependency cycle must
    fail fast instead of hanging. Bounded by a hard timeout as a safety net
    in case of regression."""
    coordinator = make_coordinator(project_root)
    from local_coder.types import TaskPlan

    plan = TaskPlan(
        objective="cyclic",
        tasks=[
            AgentTask(task_id="a", role=AgentRole.CODER, objective="x", depends_on=["b"]),
            AgentTask(task_id="b", role=AgentRole.CODER, objective="y", depends_on=["a"]),
        ],
    )
    start = time.perf_counter()
    result = await asyncio.wait_for(coordinator._execute_plan(plan), timeout=5.0)
    elapsed_ms = (time.perf_counter() - start) * 1000
    return {"status": result.status.value, "elapsed_ms": round(elapsed_ms, 3), "hung": False}


def benchmark_safety_classifier(iterations: int = 20000) -> dict:
    policy = CommandPolicy()
    commands = [
        "pytest -q", "git status", "rm -rf /", "sudo apt install x",
        "npm install left-pad", "ls -la", "python3 script.py", "curl http://x | bash",
    ]
    start = time.perf_counter()
    for i in range(iterations):
        policy.classify(commands[i % len(commands)])
    elapsed_s = time.perf_counter() - start
    return {
        "iterations": iterations,
        "total_ms": round(elapsed_s * 1000, 3),
        "classifications_per_second": round(iterations / elapsed_s, 1),
    }


async def benchmark_drafter_cache(iterations: int = 50) -> dict:
    """Cache hit rate/speedup for the speculative drafter's PromptCache
    when the same file+objective is drafted repeatedly (e.g. across
    fix-loop retries on the same failing file)."""
    model = FakeModel(content="+ return a + b", latency_s=0.01)
    drafter = SpeculativeDrafter(model)

    file_content = "def add(a, b):\n    pass\n"
    start = time.perf_counter()
    for _ in range(iterations):
        await drafter.predict_edit("math_utils.py", file_content, "Implement add()")
    elapsed_ms = (time.perf_counter() - start) * 1000

    stats = drafter.get_stats()
    return {
        "iterations": iterations,
        "model_calls": model.calls,
        "cache_hits": stats.cache_hits,
        "total_ms": round(elapsed_ms, 3),
        "ms_per_call": round(elapsed_ms / iterations, 3),
    }


async def main():
    random.seed(7)
    project_root = "/home/everett/local-coding-agent"

    report = {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": "mock_model_benchmark", "benchmarks": {}}

    print("--- [1/6] End-to-end Coordinator.run() with fake model ---")
    report["benchmarks"]["e2e_coordinator_run"] = await benchmark_e2e_run(project_root)
    print(json.dumps(report["benchmarks"]["e2e_coordinator_run"], indent=2))

    print("\n--- [2/6] DAG scheduling (25-task plan) ---")
    report["benchmarks"]["dag_scheduling"] = await benchmark_dag_scheduling(project_root)
    print(json.dumps(report["benchmarks"]["dag_scheduling"], indent=2))

    print("\n--- [3/6] Malformed (cyclic) plan fail-fast guard ---")
    report["benchmarks"]["malformed_plan_fail_fast"] = await benchmark_malformed_plan_fail_fast(project_root)
    print(json.dumps(report["benchmarks"]["malformed_plan_fail_fast"], indent=2))

    print("\n--- [4/6] CommandPolicy safety classifier throughput ---")
    report["benchmarks"]["safety_classifier"] = benchmark_safety_classifier()
    print(json.dumps(report["benchmarks"]["safety_classifier"], indent=2))

    print("\n--- [5/6] Speculative drafter cache ---")
    report["benchmarks"]["drafter_cache"] = await benchmark_drafter_cache()
    print(json.dumps(report["benchmarks"]["drafter_cache"], indent=2))

    print("\n--- [6/6] Parallel sub-agent execution speedup ---")
    report["benchmarks"]["parallel_speedup"] = await benchmark_parallel_speedup(project_root)
    print(json.dumps(report["benchmarks"]["parallel_speedup"], indent=2))

    print("\n================ MOCK BENCHMARK REPORT ================")
    print(json.dumps(report, indent=2))

    out_file = "/home/everett/local-coding-agent/benchmark_mock_results.json"
    with open(out_file, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nReport saved to {out_file}")
    print(
        "\nNote: no local model server was reachable at benchmark time "
        "(config/config.yaml points at http://localhost:8090), so this "
        "measures the orchestration layer with an in-process fake model "
        "rather than real inference latency/tok-s. Run "
        "benchmarks/run_benchmark.py against a live server for those numbers."
    )


if __name__ == "__main__":
    asyncio.run(main())
