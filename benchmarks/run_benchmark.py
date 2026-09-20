"""Benchmark suite for local-coding-agent using AI2 local models.

Measures:
1. Model server connectivity and raw inference latency / decode speed (tok/s)
2. Agent unit performance (Planner, Coder, Reviewer, Speculative Drafter)
3. Coordinator End-to-End workflow performance
"""

import sys
import os
import time
import json
import asyncio
from typing import Dict, Any

# Add AI2 and local-coding-agent src to path
sys.path.append("/home/everett/AI2")
sys.path.append("/home/everett/local-coding-agent/src")

from local_coder.orchestrator.config_loader import load_config
from local_coder.orchestrator.coordinator import Coordinator
from local_coder.models.manager import ModelManager
from local_coder.agents import create_agent
from local_coder.types import Message, AgentRole, AgentTask
from local_coder.speculative.drafter import SpeculativeDrafter


async def benchmark_model_raw(model_manager: ModelManager) -> Dict[str, Any]:
    print("\n--- [1/4] Benchmarking Raw Model Performance ---")
    model = await model_manager.get_model(AgentRole.ORCHESTRATOR)
    
    prompt = [Message(role="user", content="Write a python function to compute fibonacci numbers with memoization.")]
    start = time.time()
    response = await model.generate(prompt, max_tokens=256)
    elapsed = time.time() - start
    
    tokens = response.completion_tokens or len(response.content.split())
    tok_per_sec = tokens / elapsed if elapsed > 0 else 0
    
    result = {
        "latency_ms": response.latency_ms or (elapsed * 1000),
        "completion_tokens": tokens,
        "prompt_tokens": response.prompt_tokens,
        "tokens_per_second": round(tok_per_sec, 2),
        "model": response.model,
    }
    print(f"Raw Model Generation: {tokens} tokens in {elapsed:.2f}s ({tok_per_sec:.2f} tok/s)")
    return result


async def benchmark_speculative_drafter(model_manager: ModelManager) -> Dict[str, Any]:
    print("\n--- [2/4] Benchmarking Speculative Drafter ---")
    model = await model_manager.get_model(AgentRole.CODER)
    drafter = SpeculativeDrafter(model)
    
    context = "def solve_quadratic(a, b, c):\n    # Calculate discriminant\n"
    start = time.time()
    prediction = await drafter.predict_edit(
        file_path="quadratic.py",
        file_content=context,
        objective="Complete the function implementation.",
    )
    elapsed = time.time() - start
    draft = prediction.content if prediction else ""
    
    result = {
        "context_length": len(context),
        "draft_tokens": len(draft.split()),
        "latency_ms": elapsed * 1000,
        "draft_output": draft[:60] + "..." if len(draft) > 60 else draft,
    }
    print(f"Speculative Draft Generated in {elapsed * 1000:.2f}ms")
    return result


async def benchmark_agent_tasks(coordinator: Coordinator) -> Dict[str, Any]:
    print("\n--- [3/4] Benchmarking Individual Agent Execution ---")
    results = {}
    
    # Test Planner Agent
    planner = create_agent(
        AgentRole.PLANNER,
        await coordinator.model_manager.get_model(AgentRole.PLANNER),
        coordinator.tool_registry,
        coordinator._dispatch,
    )
    start = time.time()
    plan_task = AgentTask(
        task_id="bench_plan_1",
        role=AgentRole.PLANNER,
        objective="Create a plan for adding a helper function 'calculate_average' to utils.py",
    )
    plan_res = await planner.execute(plan_task)
    elapsed = time.time() - start
    results["planner"] = {
        "status": plan_res.status,
        "latency_ms": elapsed * 1000,
    }
    print(f"Planner Agent Execution: {elapsed:.2f}s (Status: {plan_res.status})")
        
    # Test Coder Agent
    coder = create_agent(
        AgentRole.CODER,
        await coordinator.model_manager.get_model(AgentRole.CODER),
        coordinator.tool_registry,
        coordinator._dispatch,
    )
    start = time.time()
    coder_task = AgentTask(
        task_id="bench_coder_1",
        role=AgentRole.CODER,
        objective="Write unit test function for string inversion",
    )
    coder_res = await coder.execute(coder_task)
    elapsed = time.time() - start
    results["coder"] = {
        "status": coder_res.status,
        "latency_ms": elapsed * 1000,
    }
    print(f"Coder Agent Execution: {elapsed:.2f}s (Status: {coder_res.status})")

    return results


async def benchmark_e2e_coordinator(config_path: str, project_root: str) -> Dict[str, Any]:
    print("\n--- [4/4] Benchmarking End-to-End Coordinator Workflow ---")
    config = load_config(config_path)
    coordinator = Coordinator(config=config, project_root=project_root)
    
    events = []
    coordinator.on_event(lambda e: events.append(e))
    
    start = time.time()
    result_text = await coordinator.run("Add docstring and type hints to a math function")
    elapsed = time.time() - start
    
    result = {
        "total_latency_s": round(elapsed, 2),
        "events_count": len(events),
        "output_length": len(result_text),
    }
    print(f"End-to-End Coordinator Run Complete: {elapsed:.2f}s ({len(events)} events emitted)")
    return result


async def main():
    config_path = "/home/everett/local-coding-agent/config/config.yaml"
    project_root = "/home/everett/local-coding-agent"
    
    config = load_config(config_path)
    model_manager = ModelManager(config)
    await model_manager.initialize()
    
    coordinator = Coordinator(config=config, project_root=project_root)
    
    report = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config": config_path,
        "benchmarks": {},
    }
    
    try:
        report["benchmarks"]["raw_model"] = await benchmark_model_raw(model_manager)
    except Exception as e:
        report["benchmarks"]["raw_model"] = {"error": str(e)}
        print(f"Raw model benchmark failed: {e}")
        
    try:
        report["benchmarks"]["speculative_drafter"] = await benchmark_speculative_drafter(model_manager)
    except Exception as e:
        report["benchmarks"]["speculative_drafter"] = {"error": str(e)}
        print(f"Speculative drafter benchmark failed: {e}")

    try:
        report["benchmarks"]["agent_tasks"] = await benchmark_agent_tasks(coordinator)
    except Exception as e:
        report["benchmarks"]["agent_tasks"] = {"error": str(e)}
        print(f"Agent tasks benchmark failed: {e}")

    try:
        report["benchmarks"]["e2e_coordinator"] = await benchmark_e2e_coordinator(config_path, project_root)
    except Exception as e:
        report["benchmarks"]["e2e_coordinator"] = {"error": str(e)}
        print(f"E2E coordinator benchmark failed: {e}")
        
    print("\n================ BENCHMARK REPORT ================")
    print(json.dumps(report, indent=2))
    
    # Save report
    out_file = "/home/everett/local-coding-agent/benchmark_results.json"
    with open(out_file, "w") as f:
        json.dump(report, f, indent=2)
    print(f"Report saved to {out_file}")
    
    await model_manager.close_all()


if __name__ == "__main__":
    asyncio.run(main())
